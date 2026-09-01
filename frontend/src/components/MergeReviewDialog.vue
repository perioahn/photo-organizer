<script setup lang="ts">
// 환자 폴더 합치기 검토 — 남길 폴더를 고르고 계획(이동/병합)을 확인한 뒤 실행.
// dry-run이 준 token을 그대로 제출해, 검토 후 디스크가 바뀌면 서버가 거부한다.
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import type { Patient } from '../api'

const props = defineProps<{ folders: string[]; patients: Patient[] }>()
const emit = defineEmits<{ close: []; done: [runId: string | null, msg: string] }>()

const keep = ref(props.folders[0])
const plans = ref<any[]>([])
const loading = ref(false)
const err = ref('')
const running = ref(false)

const others = computed(() => props.folders.filter((f) => f !== keep.value))
const totals = computed(() => plans.value.reduce(
  (a, p) => ({ move: a.move + (p.move ?? 0), merge: a.merge + (p.merge ?? 0) }),
  { move: 0, merge: 0 }))

function idOf(folder: string): number | undefined {
  return props.patients.find((p) => p.folder_name === folder)?.id
}

async function preview() {
  loading.value = true
  err.value = ''
  plans.value = []
  try {
    const dst = idOf(keep.value)
    if (dst === undefined) throw new Error('남길 폴더를 찾지 못했습니다 (새로고침 필요)')
    for (const f of others.value) {
      const src = idOf(f)
      if (src === undefined) throw new Error(`${f} 폴더를 찾지 못했습니다`)
      const res = await fetch('/api/patients/merge', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ src, dst, dry_run: true }),
      })
      const d = await res.json()
      if (!res.ok) throw new Error(d.detail ?? `HTTP ${res.status}`)
      plans.value.push({ ...d, srcId: src, dstId: dst })
    }
  } catch (e: any) {
    err.value = e.message ?? String(e)
  } finally {
    loading.value = false
  }
}

async function run() {
  running.value = true
  err.value = ''
  let lastRun: string | null = null
  try {
    for (const p of plans.value) {
      const res = await fetch('/api/patients/merge', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ src: p.srcId, dst: p.dstId, dry_run: false, token: p.token }),
      })
      const d = await res.json()
      if (!res.ok) throw new Error(d.detail ?? `HTTP ${res.status}`)
      lastRun = d.run_id ?? lastRun
    }
    emit('done', lastRun,
         `합침: ${totals.value.move}개 이동, ${totals.value.merge}개 같은 날짜 병합`)
    emit('close')
  } catch (e: any) {
    err.value = e.message ?? String(e)
  } finally {
    running.value = false
  }
}

watch(keep, preview)
function onKey(e: KeyboardEvent) { if (e.key === 'Escape') emit('close') }
onMounted(() => { preview(); window.addEventListener('keydown', onKey) })
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
    <div class="dlg-overlay" @click.self="emit('close')">
      <div class="dlg wide">
        <div class="dlg-head">
          <h3>환자 폴더 합치기</h3>
          <button class="icon-btn" @click="emit('close')">✕</button>
        </div>

        <div class="dlg-field">
          <span>남길 폴더 (나머지가 이쪽으로 들어옵니다)</span>
          <label v-for="f in folders" :key="f" class="dlg-radio">
            <input type="radio" :value="f" v-model="keep" /> <code>{{ f }}</code>
          </label>
        </div>

        <div v-if="loading" class="dlg-muted">검토 중…</div>
        <div v-else-if="plans.length" class="dlg-plan">
          <div v-for="p in plans" :key="p.src" class="dlg-plan-row">
            <code>{{ p.src }}</code> → <code class="new">{{ p.dst }}</code>
            <span class="dlg-muted">
              촬영일 폴더 {{ p.move }}개 이동<template v-if="p.merge">,
              같은 날짜 {{ p.merge }}개는 한 폴더로 합쳐짐</template>
            </span>
            <ul v-if="p.details?.merges?.length" class="dlg-merge-list">
              <li v-for="m in p.details.merges" :key="m[0]">
                {{ m[0] }} + {{ m[1] }} → 사진 합치고 태그 통합
              </li>
            </ul>
          </div>
        </div>

        <div v-if="err" class="dlg-err block">{{ err }}</div>

        <div class="dlg-actions">
          <span class="dlg-muted">사진은 지워지지 않고 이동만 합니다 · 되돌리기 가능</span>
          <button @click="emit('close')">취소</button>
          <button class="accent" :disabled="running || loading || !plans.length" @click="run">
            {{ running ? '합치는 중…' : `합치기 (${totals.move + totals.merge}개 폴더)` }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
