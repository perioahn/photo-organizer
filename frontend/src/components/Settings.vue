<script setup lang="ts">
import { onMounted, ref } from 'vue'

const props = defineProps<{ firstRun?: boolean }>()
const emit = defineEmits<{ close: []; saved: [] }>()

const photoRoot = ref('')
const scheduleFolders = ref<string[]>([])
const busy = ref(false)
const msg = ref('')

async function load() {
  const res = await fetch('/api/settings')
  const d = await res.json()
  photoRoot.value = d.photo_root ?? ''
  scheduleFolders.value = d.schedule_folders ?? []
}

async function pickFolder(initial: string): Promise<string | null> {
  const res = await fetch('/api/select_folder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initial }),
  })
  return (await res.json()).path
}

async function changeRoot() {
  const p = await pickFolder(photoRoot.value)
  if (p) photoRoot.value = p
}

async function changeSchedule(i: number) {
  const p = await pickFolder(scheduleFolders.value[i] ?? '')
  if (p) {
    if (i < scheduleFolders.value.length) scheduleFolders.value[i] = p
    else scheduleFolders.value.push(p)
  }
}

function removeSchedule(i: number) {
  scheduleFolders.value.splice(i, 1)
}

async function save() {
  if (!photoRoot.value) {
    msg.value = '사진 폴더를 먼저 선택하세요'
    return
  }
  busy.value = true
  msg.value = '저장 후 재스캔 중… (사진 수에 따라 수 초)'
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ photo_root: photoRoot.value, schedule_folders: scheduleFolders.value }),
    })
    if (!res.ok) throw new Error((await res.json()).detail ?? `HTTP ${res.status}`)
    emit('saved')
  } catch (e: any) {
    msg.value = `저장 실패: ${e.message ?? e}`
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="review-overlay" @click.self="!firstRun && emit('close')">
    <div class="review-panel settings-panel">
      <div class="review-head">
        <h3>{{ firstRun ? '처음 설정 — 폴더 지정' : '폴더 설정' }}</h3>
        <button v-if="!firstRun" class="icon-btn" @click="emit('close')">✕</button>
      </div>
      <p class="review-hint">
        {{ firstRun
          ? '사진 폴더를 지정해야 시작할 수 있습니다. 선택한 폴더는 저장되어 다음 실행부터 자동 적용됩니다.'
          : '변경 사항은 저장 즉시 재스캔되며 다음 실행부터 자동 적용됩니다.' }}
      </p>

      <div class="set-row">
        <label>📁 사진 폴더 (필수)</label>
        <div class="set-path">
          <code>{{ photoRoot || '(미지정)' }}</code>
          <button :disabled="busy" @click="changeRoot">선택…</button>
        </div>
      </div>

      <div class="set-row">
        <label>📄 스케줄 PDF 폴더 (선택, 최대 2)</label>
        <div v-for="(f, i) in scheduleFolders" :key="i" class="set-path">
          <code>{{ f }}</code>
          <button :disabled="busy" @click="changeSchedule(i)">변경…</button>
          <button :disabled="busy" class="rm" @click="removeSchedule(i)">×</button>
        </div>
        <div v-if="scheduleFolders.length < 2" class="set-path">
          <button :disabled="busy" @click="changeSchedule(scheduleFolders.length)">+ 폴더 추가…</button>
        </div>
      </div>

      <div class="set-actions">
        <span class="set-msg">{{ msg }}</span>
        <button class="accept" :disabled="busy || !photoRoot" @click="save">
          {{ busy ? '적용 중…' : '저장 후 재스캔' }}
        </button>
      </div>
    </div>
  </div>
</template>
