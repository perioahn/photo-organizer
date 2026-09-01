<script setup lang="ts">
// 환자 진료번호·이름 편집 — 한 화면에서 함께 수정 (Codex 자문 설계).
// 오류는 입력 옆에, 번호 충돌은 상대 환자 카드 + 합치기 유도, 편집 중 디스크 변경은 저장 차단.
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import type { Patient } from '../api'

const props = defineProps<{ patient: Patient }>()
const emit = defineEmits<{
  close: []
  saved: [runId: string | null, msg: string]
  merge: [srcId: number, dstId: number]
}>()

// 목록이 갱신돼도 초안이 날아가지 않게, 열릴 때 값만 복사해 독립 세션으로 유지
const srcFolder = props.patient.folder_name
const num = ref(props.patient.num ?? '')
const name = ref(props.patient.name ?? '')
const saving = ref(false)
const serverError = ref('')
const conflict = ref<{ id: number; folder_name: string; patient_name: string; n: number } | null>(null)
const numInput = ref<HTMLInputElement | null>(null)

const numError = computed(() => {
  const v = num.value.trim()
  if (!v) return '진료번호를 입력하세요'
  if (!/^\d{8}$/.test(v)) return '숫자 8자리여야 합니다'
  return ''
})
const nameError = computed(() => {
  const v = name.value.trim()
  if (!v) return '이름을 입력하세요'
  if (/[<>:"/\\|?*]/.test(v)) return '폴더명에 쓸 수 없는 문자가 있습니다'
  return ''
})
const preview = computed(() => `${num.value.trim()}_${name.value.trim()}`)
const changed = computed(() => preview.value !== srcFolder)
const canSave = computed(() =>
  !saving.value && !numError.value && !nameError.value && changed.value && !conflict.value)

// 번호 충돌 실시간 확인 — 늦게 온 응답이 현재 입력을 덮지 않게 seq 사용
let seq = 0
watch(num, async (v) => {
  serverError.value = ''
  const my = ++seq
  const t = v.trim()
  if (!/^\d{8}$/.test(t) || t === props.patient.num) {
    conflict.value = null
    return
  }
  try {
    const d = await (await fetch(`/api/patient_by_num/${t}`)).json()
    if (my !== seq) return
    conflict.value = d.found && d.id !== props.patient.id ? d : null
  } catch {
    if (my === seq) conflict.value = null
  }
}, { immediate: false })

async function save() {
  if (!canSave.value) return
  saving.value = true
  serverError.value = ''
  try {
    const res = await fetch(`/api/patient/${props.patient.id}/rename`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ num: num.value.trim(), name: name.value.trim(),
                             expect_folder: srcFolder }),
    })
    const d = await res.json()
    if (!res.ok) throw new Error(d.detail ?? `HTTP ${res.status}`)
    emit('saved', d.run_id, d.noop ? '변경 없음' : `${d.old} → ${d.new}`)
    emit('close')
  } catch (e: any) {
    serverError.value = e.message ?? String(e)
  } finally {
    saving.value = false
  }
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
  else if (e.key === 'Enter' && canSave.value) save()
}

onMounted(() => {
  window.addEventListener('keydown', onKey)
  setTimeout(() => numInput.value?.focus(), 0)
})
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
    <div class="dlg-overlay" @click.self="emit('close')">
      <div class="dlg">
        <div class="dlg-head">
          <h3>환자 정보 수정</h3>
          <button class="icon-btn" @click="emit('close')">✕</button>
        </div>

        <label class="dlg-field">
          <span>진료번호</span>
          <input ref="numInput" v-model="num" maxlength="8" inputmode="numeric"
                 :class="{ bad: !!numError }" placeholder="8자리" />
          <em v-if="numError" class="dlg-err">{{ numError }}</em>
        </label>

        <label class="dlg-field">
          <span>이름</span>
          <input v-model="name" :class="{ bad: !!nameError }" placeholder="환자 이름" />
          <em v-if="nameError" class="dlg-err">{{ nameError }}</em>
        </label>

        <div class="dlg-preview">
          <span class="dlg-preview-label">폴더명</span>
          <code>{{ srcFolder }}</code>
          <span v-if="changed"> → <code class="new">{{ preview }}</code></span>
          <span v-else class="dlg-muted">(변경 없음)</span>
        </div>

        <div v-if="conflict" class="dlg-conflict">
          <div><b>{{ conflict.folder_name }}</b> 폴더가 이미 이 번호를 씁니다
            ({{ conflict.patient_name }} · 촬영일 {{ conflict.n }}개)</div>
          <div class="dlg-conflict-actions">
            <button class="accent" @click="emit('merge', patient.id, conflict.id)">
              이 환자와 합치기…
            </button>
            <span class="dlg-muted">같은 사람이면 합치고, 아니면 번호를 다시 확인하세요</span>
          </div>
        </div>

        <div v-if="serverError" class="dlg-err block">{{ serverError }}</div>

        <div class="dlg-actions">
          <span class="dlg-muted">되돌리기 가능 · Enter 저장 · Esc 취소</span>
          <button @click="emit('close')">취소</button>
          <button class="accent" :disabled="!canSave" @click="save">
            {{ saving ? '저장 중…' : '저장' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
