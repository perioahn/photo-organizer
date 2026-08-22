<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, type FileEntry, type FolderNode, type Patient } from '../api'

const props = defineProps<{
  files: FileEntry[]
  startIndex: number
  folder: FolderNode
  patient: Patient
}>()
const emit = defineEmits<{ close: []; stale: [fileName: string] }>()

const index = ref(props.startIndex)
const loaded = ref(false)
const failed = ref(false)

const current = computed(() => props.files[index.value])

function go(delta: number) {
  const next = index.value + delta
  if (next < 0 || next >= props.files.length) return
  loaded.value = false
  failed.value = false
  index.value = next
}

// 복구(App이 새 파일 목록 주입) 시 내부 상태 리셋 — 에러 화면에서 자동 탈출
watch(() => props.files, () => {
  index.value = Math.min(props.startIndex, props.files.length - 1)
  loaded.value = false
  failed.value = false
})

// 이웃 이미지 미리 당겨오기 — 좌우 이동이 즉각 반응하도록
watch(index, () => {
  for (const d of [1, -1]) {
    const f = props.files[index.value + d]
    if (f) new Image().src = api.imageUrl(f.id)
  }
}, { immediate: true })

// 폴더 리네임(태그 작업·타 PC 동기화) 후 재스캔으로 파일 id가 바뀌면 옛 id는 영구 404.
// 실패 시 App에 알려 새 인덱스 기준으로 뷰어를 재구성한다 (5초 쿨다운, 무한루프 방지).
let lastStale = 0
function onImgError() {
  failed.value = true
  const now = Date.now()
  if (now - lastStale > 5000) {
    lastStale = now
    emit('stale', current.value?.name ?? '')
  }
}

function retry() {
  failed.value = false
  emit('stale', current.value?.name ?? '')
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
  else if (e.key === 'ArrowRight') go(1)
  else if (e.key === 'ArrowLeft') go(-1)
}

onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="viewer" @click.self="emit('close')">
    <div class="viewer-top">
      <span>{{ patient.name }} / {{ folder.name }} — {{ current.name }} ({{ index + 1 }}/{{ files.length }})</span>
      <button class="icon-btn" @click="emit('close')">✕</button>
    </div>
    <button class="nav prev" :disabled="index === 0" @click="go(-1)">‹</button>
    <div class="viewer-stage">
      <img
        v-if="!failed"
        :key="current.id"
        :src="api.imageUrl(current.id)"
        :class="{ dim: !loaded }"
        @load="loaded = true"
        @error="onImgError"
      />
      <div v-else class="viewer-error">
        원본을 불러올 수 없습니다 (파일 이동/동기화 중?)
        <button @click="retry">재시도</button>
      </div>
      <div v-if="!loaded && !failed" class="spinner" />
    </div>
    <button class="nav next" :disabled="index === files.length - 1" @click="go(1)">›</button>
    <div class="viewer-strip">
      <img
        v-for="(f, i) in files"
        :key="f.id"
        :src="api.thumbUrl(f.id)"
        :class="{ active: i === index }"
        loading="lazy"
        @click="index = i; loaded = false; failed = false"
      />
    </div>
  </div>
</template>
