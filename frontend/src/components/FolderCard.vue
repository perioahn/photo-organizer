<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, loadTagRanks, sortTagsForDisplay, type FileEntry, type FolderNode } from '../api'
import TagPicker from './TagPicker.vue'

const props = defineProps<{
  folder: FolderNode
  nested?: boolean
  showSub?: boolean // 하위 폴더 전역 펼침 (기본 접힘)
  patientLabel?: string // 날짜순 평면 목록에서 카드에 환자 표기
}>()
const emit = defineEmits<{
  open: [payload: { files: FileEntry[]; index: number; folder: FolderNode }]
  pdf: [date6: string | null]
  written: [runId: string | null, msg: string]
}>()

const adding = ref(false)
const anchorRect = ref<DOMRect | null>(null)
const busy = ref(false)

const displayTags = computed(() => sortTagsForDisplay(props.folder.tags))

function togglePicker(e: MouseEvent) {
  if (adding.value) {
    adding.value = false
    return
  }
  anchorRect.value = (e.currentTarget as HTMLElement).getBoundingClientRect()
  adding.value = true
}

async function applyTags(add: string[], remove: string[]) {
  if (busy.value) return
  busy.value = true
  const before = [...props.folder.tags]
  // optimistic: 화면 먼저 반영, 실패 시 롤백
  props.folder.tags = before.filter((t) => !remove.includes(t)).concat(add)
  try {
    const r = await api.editTags(props.folder.id, add, remove)
    props.folder.name = r.new
    emit('written', r.run_id, `${r.old} → ${r.new}`)
    if (add.length) loadTagRanks(true).catch(() => {}) // 새 태그 → 피커/표기순서 즉시 갱신
  } catch (e: any) {
    props.folder.tags = before
    emit('written', null, `태그 변경 실패: ${e.message ?? e}`)
  } finally {
    busy.value = false
  }
}

function onPick(tag: string) {
  applyTags([tag], [])
  // 피커는 열어둠 — 치식처럼 연속 추가가 흔함. Esc/바깥클릭으로 닫음.
}

const files = ref<FileEntry[]>([])
const state = ref<'idle' | 'loading' | 'ready' | 'error'>('idle')
const el = ref<HTMLElement | null>(null)
let observer: IntersectionObserver | null = null

async function loadFiles() {
  if (state.value === 'loading' || state.value === 'ready') return
  state.value = 'loading'
  try {
    files.value = (await api.folderFiles(props.folder.id)).filter((f) => f.kind !== 'video')
    state.value = 'ready'
  } catch {
    state.value = 'error' // 카드 단위 실패 — 앱은 계속 동작, 재시도 버튼 제공
  }
}

onMounted(() => {
  observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        loadFiles()
        observer?.disconnect()
      }
    },
    { rootMargin: '400px 0px' },
  )
  if (el.value) observer.observe(el.value)
})

onUnmounted(() => observer?.disconnect())

function fmtDate(d: string | null): string {
  if (!d) return ''
  return `20${d.slice(0, 2)}-${d.slice(2, 4)}-${d.slice(4, 6)}`
}

function openAt(i: number) {
  emit('open', { files: files.value, index: i, folder: props.folder })
}

// 하위 폴더: 기본 접힘, 전역 토글 따라가되 카드에서 개별 펼침도 가능
const subOpen = ref(props.showSub ?? false)
watch(() => props.showSub, (v) => { subOpen.value = v ?? false })
</script>

<template>
  <div ref="el" class="folder-card" :class="{ irregular: !folder.is_regular, nested }">
    <div class="folder-head">
      <span v-if="patientLabel" class="fpatient">{{ patientLabel }}</span>
      <span v-if="folder.date6" class="fdate">{{ fmtDate(folder.date6) }}</span>
      <span v-else class="fname">{{ folder.name }}</span>
      <span
        v-for="t in displayTags"
        :key="t"
        class="tag"
        :class="{ tooth: t.startsWith('#') }"
      >{{ t }}<span class="tag-x" title="태그 제거" @click.stop="applyTags([], [t])">×</span></span>
      <span v-if="folder.date6" class="tag-add-wrap">
        <button class="tag add" title="태그 추가" @click.stop="togglePicker">+</button>
        <TagPicker
          v-if="adding && anchorRect"
          :existing="folder.tags"
          :anchor="anchorRect"
          @pick="onPick"
          @close="adding = false"
        />
      </span>
      <span class="spacer" />
      <span class="fcount">{{ folder.image_count }}장</span>
      <button
        v-if="folder.date6"
        class="icon-btn"
        title="이 진료일 차트 PDF 열기 (다음 내원 우선)"
        @click.stop="emit('pdf', folder.date6)"
      >📄</button>
      <button class="icon-btn" title="탐색기에서 열기" @click.stop="api.openFolder(folder.id)">📂</button>
    </div>

    <div v-if="state === 'ready' && files.length" class="thumb-grid">
      <img
        v-for="(f, i) in files"
        :key="f.id"
        :src="api.thumbUrl(f.id)"
        :alt="f.name"
        loading="lazy"
        decoding="async"
        @click="openAt(i)"
        @error="($event.target as HTMLImageElement).classList.add('broken')"
      />
    </div>
    <div v-else-if="state === 'ready'" class="thumb-empty">이미지 없음</div>
    <div v-else-if="state === 'error'" class="thumb-empty">
      로드 실패 <button @click="state = 'idle'; loadFiles()">재시도</button>
    </div>
    <div v-else class="thumb-skeleton" :style="{ '--n': Math.min(folder.image_count, 8) }">
      <div v-for="i in Math.min(folder.image_count || 1, 8)" :key="i" class="skel" />
    </div>

    <button
      v-if="folder.children.length"
      class="sub-toggle"
      @click.stop="subOpen = !subOpen"
    >{{ subOpen ? '▾' : '▸' }} 하위 폴더 {{ folder.children.length }}개</button>
    <template v-if="subOpen">
      <FolderCard
        v-for="c in folder.children"
        :key="c.id"
        :folder="c"
        nested
        :show-sub="showSub"
        @open="(p) => emit('open', p)"
        @pdf="(d) => emit('pdf', d)"
        @written="(r, m) => emit('written', r, m)"
      />
    </template>
  </div>
</template>
