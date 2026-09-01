<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, connectEvents, loadTagRanks, type FileEntry, type FolderNode, type Patient } from './api'
import FolderCard from './components/FolderCard.vue'
import ImportWizard from './components/ImportWizard.vue'
import MergeReviewDialog from './components/MergeReviewDialog.vue'
import PatientEditDialog from './components/PatientEditDialog.vue'
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

// 날짜순 = 촬영일 폴더의 평면 목록 (환자 묶음 없이 말 그대로 촬영일 역순)
const flatFolders = computed(() => {
  const out: { p: Patient; f: FolderNode }[] = []
  for (const p of patients.value)
    for (const f of p.folders) if (f.date6) out.push({ p, f })
  return out.sort((a, b) =>
    b.f.date6!.localeCompare(a.f.date6!) || a.p.name.localeCompare(b.p.name))
})

// 하위 폴더 표시 (사용자가 촬영일 폴더 안에 편집본/CT 폴더를 파는 경우) — 기본 접힘
const showSub = ref(localStorage.getItem('showSub') === '1')
function toggleSub() {
  showSub.value = !showSub.value
  localStorage.setItem('showSub', showSub.value ? '1' : '0')
}

// 의심 폴더 경고 — 진료번호 오타로 갈라진 환자 폴더 (구 앱 기능)
interface SuspGroup { name?: string; num?: string; folders: string[] }
const susp = ref<{ case1: SuspGroup[]; case2: SuspGroup[] }>({ case1: [], case2: [] })
const suspOpen = ref(false)
const suspCount = computed(() => susp.value.case1.length + susp.value.case2.length)

async function loadSuspicious() {
  try { susp.value = await (await fetch('/api/suspicious')).json() } catch { /* 무시 */ }
}

function searchFolder(folder: string) {
  query.value = folder
  load()
}

// 편집 다이얼로그 상태 (목록이 갱신돼도 유지되도록 App 레벨에서 관리)
const editing = ref<Patient | null>(null)
const merging = ref<string[] | null>(null)

function editPatient(p: Patient) {
  editing.value = p
}

function onEdited(runId: string | null, msg: string) {
  if (runId) lastRunId.value = runId
  statusMsg.value = msg
  load()
  loadSuspicious()
}

async function openMerge(folders: string[]) {
  // 검색 중이면 목록에 상대 환자가 없을 수 있어 전체 트리를 확보한다
  if (!folders.every((f) => patients.value.some((p) => p.folder_name === f))) {
    allPatients.value = await api.tree()
  } else {
    allPatients.value = patients.value
  }
  merging.value = folders
}

const allPatients = ref<Patient[]>([])

function mergeFromDialog(srcId: number, dstId: number) {
  const byId = new Map(patients.value.map((p) => [p.id, p.folder_name]))
  const a = byId.get(srcId)
  const b = byId.get(dstId)
  editing.value = null
  if (a && b) openMerge([b, a])  // 남길 후보를 상대(기존 번호 소유자) 먼저
}

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

function scrollToPatient(p: Patient) {
  // 날짜순 평면 목록에는 환자 섹션이 없으니 검색으로 이동
  if (sortMode.value === 'recent') {
    query.value = p.num ?? p.name
    load()
    return
  }
  document.getElementById(`patient-${p.id}`)?.scrollIntoView({ block: 'start' })
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
  loadSuspicious()
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
      loadSuspicious()
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

// 뷰어가 들고 있는 파일 id가 낡았을 때(폴더 리네임 후 재스캔) 새 id로 재구성
async function onViewerStale(fileName: string) {
  const cur = viewer.value
  if (!cur) return
  await load()
  const p = patients.value.find((x) => x.folder_name === cur.patient.folder_name)
  const f = p?.folders.find((x) => x.name === cur.folder.name)
    ?? p?.folders.find((x) => x.date6 && x.date6 === cur.folder.date6)
  if (!p || !f) {
    viewer.value = null
    statusMsg.value = '폴더가 변경되어 목록을 새로고침했습니다'
    return
  }
  try {
    const files = (await api.folderFiles(f.id)).filter((x) => x.kind !== 'video')
    if (!files.length) throw new Error('빈 폴더')
    const idx = Math.max(0, files.findIndex((x) => x.name === fileName))
    viewer.value = { files, index: idx, folder: f, patient: p }
    statusMsg.value = '변경된 폴더를 다시 불러왔습니다'
  } catch {
    viewer.value = null
    statusMsg.value = '폴더가 변경되어 목록을 새로고침했습니다'
  }
}

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
          placeholder="검색 (/ 로 포커스): 이름·번호·태그·#치식·날짜(251020)"
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
          @click="scrollToPatient(p)"
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
        <button class="sub-global-toggle" :class="{ on: showSub }"
                title="촬영일 폴더 안의 하위 폴더(편집본·CT 등) 표시" @click="toggleSub">
          {{ showSub ? '▾' : '▸' }} 하위폴더
        </button>
        <span v-if="loading">불러오는 중…</span>
        <span v-else-if="error" class="err">{{ error }}</span>
        <span v-else>{{ patients.length }}명 · 폴더 {{ totalFolders }} · {{ statusMsg }}</span>
        <button v-if="lastRunId" class="undo-btn" title="마지막 변경 되돌리기" @click="undoLast">↩ 되돌리기</button>
      </div>
    </aside>

    <main class="content">
      <div v-if="suspCount" class="susp-banner">
        <button class="susp-head" @click="suspOpen = !suspOpen">
          ⚠ 진료번호 오타 의심 {{ suspCount }}건 {{ suspOpen ? '▾' : '▸' }}
        </button>
        <div v-if="suspOpen" class="susp-body">
          <div v-for="g in susp.case1" :key="'n' + g.name" class="susp-item">
            <span class="susp-kind">이름 같고 번호 다름</span>
            <span class="susp-why">{{ g.name }}</span>
            <button v-for="f in g.folders" :key="f" class="susp-folder" @click="searchFolder(f)">{{ f }}</button>
            <button class="susp-merge" @click="openMerge(g.folders)">합치기…</button>
          </div>
          <div v-for="g in susp.case2" :key="'c' + g.num" class="susp-item">
            <span class="susp-kind alt">번호 같고 이름 다름</span>
            <span class="susp-why">{{ g.num }}</span>
            <button v-for="f in g.folders" :key="f" class="susp-folder" @click="searchFolder(f)">{{ f }}</button>
          </div>
        </div>
      </div>
      <div v-if="error" class="error-banner">
        {{ error }} <button @click="load()">재시도</button>
      </div>
      <!-- 날짜순: 환자 묶음 없이 촬영일 역순 평면 목록 -->
      <div v-if="sortMode === 'recent'" class="folders flat-by-date">
        <FolderCard
          v-for="e in flatFolders"
          :key="e.f.id"
          :folder="e.f"
          :show-sub="showSub"
          :patient-label="`${e.p.name} ${e.p.num ?? ''}`.trim()"
          @open="(payload) => openViewer(payload, e.p)"
          @pdf="(date6) => openPdf(e.p.num, date6)"
          @written="onWritten"
          @edit-patient="editPatient(e.p)"
        />
      </div>
      <!-- 이름순: 기존 환자 섹션 -->
      <template v-else>
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
            <button class="icon-btn" title="진료번호·이름 수정" @click="editPatient(p)">✏</button>
          </h2>
          <div class="folders">
            <FolderCard
              v-for="f in p.folders"
              :key="f.id"
              :folder="f"
              :show-sub="showSub"
              @open="(payload) => openViewer(payload, p)"
              @pdf="(date6) => openPdf(p.num, date6)"
              @written="onWritten"
            />
          </div>
        </section>
      </template>
      <p v-if="!loading && !patients.length" class="empty">결과 없음</p>
    </main>

    <Viewer
      v-if="viewer"
      :files="viewer.files"
      :start-index="viewer.index"
      :folder="viewer.folder"
      :patient="viewer.patient"
      @close="onViewerClose"
      @stale="onViewerStale"
    />
    <Settings
      v-if="showSettings"
      :first-run="firstRun"
      @close="showSettings = false"
      @saved="onSettingsSaved"
    />
    <PatientEditDialog
      v-if="editing"
      :patient="editing"
      @close="editing = null"
      @saved="onEdited"
      @merge="mergeFromDialog"
    />
    <MergeReviewDialog
      v-if="merging"
      :folders="merging"
      :patients="allPatients.length ? allPatients : patients"
      @close="merging = null"
      @done="onEdited"
    />
    <ImportWizard
      v-if="showImport"
      @close="showImport = false; checkImportSession()"
      @committed="(m) => { statusMsg = m; load() }"
    />
  </div>
</template>
