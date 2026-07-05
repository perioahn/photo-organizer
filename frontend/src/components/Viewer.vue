<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, type FileEntry, type FolderNode, type Patient } from '../api'

const props = defineProps<{
  files: FileEntry[]
  startIndex: number
  folder: FolderNode
  patient: Patient
}>()
const emit = defineEmits<{ close: [] }>()

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

// 이웃 이미지 미리 당겨오기 — 좌우 이동이 즉각 반응하도록
watch(index, () => {
  for (const d of [1, -1]) {
    const f = props.files[index.value + d]
    if (f) new Image().src = api.imageUrl(f.id)
  }
}, { immediate: true })

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
        @error="failed = true"
      />
      <div v-else class="viewer-error">
        원본을 불러올 수 없습니다 (파일 이동/동기화 중?)
        <button @click="failed = false">재시도</button>
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
