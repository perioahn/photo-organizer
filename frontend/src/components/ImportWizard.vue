<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { connectEvents } from '../api'

const emit = defineEmits<{ close: []; committed: [msg: string] }>()

interface Item { name: string; kind: 'info' | 'clinical'; num: string | null; ts: number }
interface Group {
  id: number; num: string | null; name: string | null; date6: string | null
  enabled: boolean; info_idx: number | null; item_idxs: number[]; unassigned?: boolean
}
interface Session {
  status: string; folder?: string; items?: Item[]; groups?: Group[]
  progress?: { done: number; total: number }; report?: any[]; run_id?: string; error?: string
}

const session = ref<Session>({ status: 'none' })
const selectedGid = ref<number | null>(null)
const busy = ref(false)
const msg = ref('')
let es: EventSource | null = null

const groups = computed(() => session.value.groups ?? [])
const selected = computed(() => groups.value.find((g) => g.id === selectedGid.value) ?? null)
const items = computed(() => session.value.items ?? [])
const enabledStats = computed(() => {
  const gs = groups.value.filter((g) => g.enabled && !g.unassigned)
  return { groups: gs.length, photos: gs.reduce((n, g) => n + g.item_idxs.length, 0) }
})

async function refresh() {
  session.value = await (await fetch('/api/import/session')).json()
  if (session.value.status === 'review' && groups.value.length && selectedGid.value === null) {
    selectedGid.value = groups.value.find((g) => !g.unassigned)?.id ?? groups.value[0].id
  }
}

async function pickAndStart() {
  busy.value = true
  msg.value = '폴더 선택 창을 확인하세요…'
  try {
    const sel = await (await fetch('/api/select_folder', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })).json()
    if (!sel.path) { msg.value = ''; return }
    const res = await fetch('/api/import/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder: sel.path }),
    })
    if (!res.ok) throw new Error((await res.json()).detail ?? `HTTP ${res.status}`)
    msg.value = '사진 분류 중…'
    await refresh()
  } catch (e: any) {
    msg.value = `시작 실패: ${e.message ?? e}`
  } finally {
    busy.value = false
  }
}

async function patchGroup(g: Group, fields: Record<string, unknown>) {
  const res = await fetch(`/api/import/group/${g.id}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  })
  if (res.ok) Object.assign(g, await res.json())
}

async function onNumInput(g: Group) {
  if (g.num && /^\d{8}$/.test(g.num)) {
    const d = await (await fetch(`/api/patient_name/${g.num}`)).json()
    await patchGroup(g, { num: g.num, name: d.name ?? g.name })
  }
}

async function itemAction(idx: number, action: 'promote' | 'demote') {
  busy.value = true
  try {
    const res = await fetch(`/api/import/item/${idx}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    })
    if (!res.ok) throw new Error((await res.json()).detail ?? '실패')
    session.value = await res.json()
  } finally {
    busy.value = false
  }
}

async function commit() {
  const st = enabledStats.value
  if (!window.confirm(
    `선택된 ${st.groups}개 묶음(${st.photos}장)을 사진정리 폴더로 복사할까요?\n` +
    '원본은 그대로 두고 복사만 하며, 되돌리기가 가능합니다.')) return
  busy.value = true
  msg.value = '복사 중…'
  try {
    const res = await fetch('/api/import/commit', { method: 'POST' })
    const d = await res.json()
    if (!res.ok) throw new Error(d.detail ?? `HTTP ${res.status}`)
    await refresh()
    emit('committed', `가져오기 완료: ${d.copied}장 복사 (되돌리기 가능)`)
  } catch (e: any) {
    msg.value = `복사 실패: ${e.message ?? e}`
    await refresh()
  } finally {
    busy.value = false
  }
}

async function discard() {
  if (!window.confirm('이 가져오기 세션을 폐기할까요? (원본 사진은 그대로 남습니다)')) return
  await fetch('/api/import/discard', { method: 'POST' })
  selectedGid.value = null
  await refresh()
}

function groupLabel(g: Group): string {
  if (g.unassigned) return `⚠ 미분류 (${g.item_idxs.length}장)`
  const who = g.num ? `${g.num}${g.name ? '_' + g.name : ''}` : '번호 미인식'
  return `${g.date6 ?? '??'} · ${who} · ${g.item_idxs.length}장`
}

onMounted(async () => {
  await refresh()
  es = connectEvents({
    import: (d) => {
      if (d.state === 'ocr') msg.value = `진료번호 인식 중 ${d.done}/${d.total}`
      else if (d.state === 'classified') msg.value = `${d.files}장 분류됨, 정보사진 ${d.info}장 인식 중…`
      else if (d.state === 'review') { msg.value = ''; refresh() }
      else if (d.state === 'error') { msg.value = `오류: ${d.detail}`; refresh() }
    },
  })
})
onUnmounted(() => es?.close())
</script>

<template>
  <div class="review-overlay" @click.self="emit('close')">
    <div class="review-panel import-panel">
      <div class="review-head">
        <h3>📥 새 사진 추가</h3>
        <div>
          <button v-if="session.status !== 'none'" class="gen-btn" @click="discard">세션 폐기</button>
          <button class="icon-btn" @click="emit('close')">✕</button>
        </div>
      </div>

      <!-- 시작 -->
      <template v-if="session.status === 'none' || session.status === 'error'">
        <p class="review-hint">
          카메라/SD카드 폴더를 선택하면 정보사진(모니터 촬영)과 임상사진을 자동 구분하고
          진료번호를 인식합니다. 검토·수정 후에 복사가 이뤄집니다.
        </p>
        <p v-if="session.error" class="review-hint" style="color:#ffb4ab">이전 오류: {{ session.error }}</p>
        <div class="set-actions">
          <span class="set-msg">{{ msg }}</span>
          <button class="accept" :disabled="busy" @click="pickAndStart">📁 폴더 선택…</button>
        </div>
      </template>

      <!-- 분류/OCR 진행 -->
      <template v-else-if="session.status === 'scanning'">
        <p class="review-hint">{{ msg || '분석 중…' }}</p>
      </template>

      <!-- 완료 -->
      <template v-else-if="session.status === 'done'">
        <p class="review-hint">복사 완료. 되돌리려면 상태바의 ↩ 버튼을 사용하세요.</p>
        <ul class="import-report">
          <li v-for="r in session.report" :key="r.group">
            {{ r.patient }} / {{ r.folder }} — {{ r.copied }}장
          </li>
        </ul>
        <div class="set-actions">
          <button class="accept" @click="discard(); emit('close')">닫기</button>
        </div>
      </template>

      <!-- 리뷰 -->
      <template v-else-if="session.status === 'review' || session.status === 'committing'">
        <p class="review-hint">
          묶음을 검토하세요: 번호/이름/날짜 수정, 묶음 제외, 임상사진의 ✂로 묶음 분리.
          <span v-if="msg"> · {{ msg }}</span>
        </p>
        <div class="import-body">
          <div class="import-groups">
            <div
              v-for="g in groups"
              :key="g.id"
              class="import-group-item"
              :class="{ active: g.id === selectedGid, off: !g.enabled }"
              @click="selectedGid = g.id"
            >
              <input type="checkbox" :checked="g.enabled" :disabled="g.unassigned"
                     @click.stop @change="patchGroup(g, { enabled: ($event.target as HTMLInputElement).checked })" />
              <span>{{ groupLabel(g) }}</span>
            </div>
          </div>

          <div v-if="selected" class="import-detail">
            <div v-if="selected.info_idx !== null" class="import-info-photo">
              <img :src="`/api/import/image/${selected.info_idx}`" alt="정보사진" />
            </div>
            <div class="import-fields">
              <label>진료번호
                <input v-model="selected.num" maxlength="8" placeholder="8자리"
                       @input="onNumInput(selected)" @blur="patchGroup(selected, { num: selected.num })" />
              </label>
              <label>이름
                <input v-model="selected.name" placeholder="신규 환자는 입력 필요"
                       @blur="patchGroup(selected, { name: selected.name })" />
              </label>
              <label>날짜(YYMMDD)
                <input v-model="selected.date6" maxlength="6"
                       @blur="patchGroup(selected, { date6: selected.date6 })" />
              </label>
              <span v-if="selected.num && selected.name" class="known-badge">
                {{ selected.name }} — 폴더: {{ selected.num }}_{{ selected.name }}/{{ selected.date6 }}
              </span>
              <span v-else-if="selected.num" class="warn-badge">
                ⚠ 기존 환자 없음 — 번호를 확인하세요 (신규 환자면 이름 입력)
              </span>
            </div>
            <div class="import-thumbs">
              <div v-for="i in selected.item_idxs" :key="i" class="import-thumb"
                   :class="{ info: items[i]?.kind === 'info' }">
                <img :src="`/api/import/thumb/${i}`" loading="lazy" />
                <div class="thumb-tools">
                  <span v-if="items[i]?.kind === 'info'" class="badge">정보</span>
                  <button v-if="items[i]?.kind === 'clinical'" title="여기부터 새 묶음"
                          @click="itemAction(i, 'promote')">✂</button>
                  <button v-else-if="i !== selected.info_idx || selected.item_idxs.length === 1"
                          title="임상사진으로 변경" @click="itemAction(i, 'demote')">↩임상</button>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div class="set-actions">
          <span class="set-msg">선택 {{ enabledStats.groups }}묶음 · {{ enabledStats.photos }}장</span>
          <button class="accept" :disabled="busy || !enabledStats.groups" @click="commit">
            사진정리 폴더로 복사
          </button>
        </div>
      </template>
    </div>
  </div>
</template>
