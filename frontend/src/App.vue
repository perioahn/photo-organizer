<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, connectEvents, loadTagRanks, type FileEntry, type FolderNode, type Patient } from './api'
import FolderCard from './components/FolderCard.vue'
import ImportWizard from './components/ImportWizard.vue'
import Settings from './components/Settings.vue'
import Viewer from './components/Viewer.vue'

const patients = ref<Patient[]>([])
const query = ref('')
const loading = ref(false)
const error = ref('')
const statusMsg = ref('')
const searchBox = ref<HTMLInputElement | null>(null)

const viewer = ref<{ files: FileEntry[]; index: number; folder: FolderNode; patient: Patient } | null>(null)
const lastRunId = ref<string | null>(null)
const showSettings = ref(false)
const firstRun = ref(false)
const showImport = ref(false)
const importPending = ref(false)

async function checkImportSession() {
  try {
    const s = await (await fetch('/api/import/session')).json()
    importPending.value = ['scanning', 'review'].includes(s.status)
  } catch { /* ignore */ }
}

async function checkRoot() {
  try {
    const res = await fetch('/api/settings')
    const d = await res.json()
    if (!d.root_ok) {
      firstRun.value = true
      showSettings.value = true
    }
  } catch { /* 서버 기동 중 */ }
}

function onSettingsSaved() {
  firstRun.value = false
  showSettings.value = false
  load()
  loadTagRanks(true).catch(() => {})
  statusMsg.value = '폴더 설정 적용됨'
}

function onWritten(runId: string | null, msg: string) {
  statusMsg.value = msg
  if (runId) lastRunId.value = runId
}

async function undoLast() {
  if (!lastRunId.value) return
  try {
    const r = await api.undo(lastRunId.value)
    statusMsg.value = `되돌림: ${r.reverted}건`
    lastRunId.value = null
  } catch (e: any) {
    statusMsg.value = `되돌리기 실패: ${e.message ?? e}`
  }
}

let debounceTimer: number | undefined
let es: EventSource | null = null
let refreshQueued = false
let loadSeq = 0

const totalFolders = computed(() =>
  patients.value.reduce((n, p) => n + p.folders.length, 0),
)

// 기본 정렬: 최근 촬영일 순 (환자의 가장 최근 정규 폴더 날짜 기준)
const sortMode = ref<'recent' | 'name'>(
  (localStorage.getItem('sortMode') as 'recent' | 'name') ?? 'recent',
)

function toggleSort() {
  sortMode.value = sortMode.value === 'recent' ? 'name' : 'recent'
  localStorage.setItem('sortMode', sortMode.value)
}

function latestDate(p: Patient): string {
  let max = ''
  for (const f of p.folders) if (f.date6 && f.date6 > max) max = f.date6
  return max
}

const sortedPatients = computed(() => {
  if (sortMode.value === 'name') return patients.value
  return [...patients.value].sort((a, b) => latestDate(b).localeCompare(latestDate(a)))
})

async function load(q = query.value) {
  const seq = ++loadSeq // 늦게 도착한 이전 응답이 최신 결과를 덮지 않게
  loading.value = true
  error.value = ''
  try {
    const result = q.trim() ? await api.search(q) : await api.tree()
    if (seq !== loadSeq) return
    patients.value = result
  } catch (e: any) {
    if (seq !== loadSeq) return
    error.value = `불러오기 실패: ${e.message ?? e}`
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

async function openPdf(num: string | null, date6?: string | null) {
  if (!num) {
    statusMsg.value = '진료번호 없는 폴더 — PDF 검색 불가'
    return
  }
  try {
    const hits = await api.patientPdfs(num, date6 ?? undefined)
    if (!hits.length) {
      statusMsg.value = `진료번호 ${num} 스케줄 PDF 없음`
      return
    }
    const best = hits[0]
    window.open(api.pdfUrl(best.pdf_id, best.page), '_blank')
    statusMsg.value = `PDF 열림: ${best.filename} p.${best.page} (${best.kind})`
  } catch (e: any) {
    statusMsg.value = `PDF 검색 실패: ${e.message ?? e}`
  }
}

function onSearchInput() {
  window.clearTimeout(debounceTimer)
  debounceTimer = window.setTimeout(() => load(), 250)
}

function clearSearch() {
  query.value = ''
  load('')
}

function openViewer(payload: { files: FileEntry[]; index: number; folder: FolderNode }, patient: Patient) {
  viewer.value = { ...payload, patient }
}

function scrollToPatient(id: number) {
  document.getElementById(`patient-${id}`)?.scrollIntoView({ block: 'start' })
}

function onKey(e: KeyboardEvent) {
  if (viewer.value) return // 뷰어가 키를 소유
  if (e.key === '/' && document.activeElement !== searchBox.value) {
    e.preventDefault()
    searchBox.value?.focus()
  } else if (e.key === 'Escape' && query.value) {
    clearSearch()
  }
}

onMounted(() => {
  load()
  loadTagRanks().catch(() => {})
  checkRoot()
  checkImportSession()
  window.addEventListener('keydown', onKey)
  es = connectEvents({
    index: (d) => {
      statusMsg.value = `인덱스 갱신됨 (${d.patients}명/${d.files}장, ${d.seconds}s)`
      // 뷰어 사용 중이면 닫힐 때까지 미룸 — 작업 중 새로고침으로 방해하지 않기
      if (viewer.value) refreshQueued = true
      else load()
    },
    prewarm: (d) => {
      statusMsg.value =
        d.state === 'done' ? '썸네일 준비 완료' : `썸네일 준비 중 ${d.done ?? 0}/${d.total}`
    },
  })
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKey)
  es?.close()
})

function onViewerClose() {
  viewer.value = null
  if (refreshQueued) {
    refreshQueued = false
    load()
  }
}
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="search-wrap">
        <input
          ref="searchBox"
          v-model="query"
          placeholder="검색 (/ 로 포커스): 이름·번호·태그·#치식"
          @input="onSearchInput"
          @keydown.escape.stop="clearSearch"
        />
        <button v-if="query" class="clear" @click="clearSearch">×</button>
      </div>
      <div class="sort-row">
        <button class="sort-btn" @click="toggleSort">
          {{ sortMode === 'recent' ? '📅 최근 촬영순' : '🔤 이름순' }} ⇄
        </button>
      </div>
      <div class="patient-list">
        <div
          v-for="p in sortedPatients"
          :key="p.id"
          class="patient-item"
          @click="scrollToPatient(p.id)"
        >
          <span class="pname">{{ p.name }}</span>
          <span class="pnum">{{ p.num ?? '' }}</span>
          <span v-if="sortMode === 'recent' && latestDate(p)" class="pdate">{{ latestDate(p) }}</span>
          <span class="pcount">{{ p.folders.length }}</span>
        </div>
      </div>
      <div class="review-launcher">
        <button class="review-btn settings-launch" :class="{ hot: importPending }"
                title="카메라 폴더에서 새 사진 가져오기" @click="showImport = true">
          📥 새 사진 추가{{ importPending ? ' (이어서 하기)' : '' }}
        </button>
        <button class="review-btn settings-launch" title="사진/스케줄 폴더 설정" @click="showSettings = true">
          ⚙ 폴더 설정
        </button>
      </div>
      <div class="statusbar">
        <span v-if="loading">불러오는 중…</span>
        <span v-else-if="error" class="err">{{ error }}</span>
        <span v-else>{{ patients.length }}명 · 폴더 {{ totalFolders }} · {{ statusMsg }}</span>
        <button v-if="lastRunId" class="undo-btn" title="마지막 변경 되돌리기" @click="undoLast">↩ 되돌리기</button>
      </div>
    </aside>

    <main class="content">
      <div v-if="error" class="error-banner">
        {{ error }} <button @click="load()">재시도</button>
      </div>
      <section
        v-for="p in sortedPatients"
        :id="`patient-${p.id}`"
        :key="p.id"
        class="patient-section"
      >
        <h2>
          {{ p.name }} <span class="pnum">{{ p.num ?? p.folder_name }}</span>
          <button
            v-if="p.num"
            class="icon-btn"
            title="스케줄 PDF 열기 (최신)"
            @click="openPdf(p.num)"
          >📄</button>
        </h2>
        <div class="folders">
          <FolderCard
            v-for="f in p.folders"
            :key="f.id"
            :folder="f"
            @open="(payload) => openViewer(payload, p)"
            @pdf="(date6) => openPdf(p.num, date6)"
            @written="onWritten"
          />
        </div>
      </section>
      <p v-if="!loading && !patients.length" class="empty">결과 없음</p>
    </main>

    <Viewer
      v-if="viewer"
      :files="viewer.files"
      :start-index="viewer.index"
      :folder="viewer.folder"
      :patient="viewer.patient"
      @close="onViewerClose"
    />
    <Settings
      v-if="showSettings"
      :first-run="firstRun"
      @close="showSettings = false"
      @saved="onSettingsSaved"
    />
    <ImportWizard
      v-if="showImport"
      @close="showImport = false; checkImportSession()"
      @committed="(m) => { statusMsg = m; load() }"
    />
  </div>
</template>
