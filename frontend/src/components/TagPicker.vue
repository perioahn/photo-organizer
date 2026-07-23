<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { fetchTagGroups, loadTagRanks, tagRanks, type TagGroup } from '../api'

const props = defineProps<{ existing: string[]; anchor: DOMRect }>()
const emit = defineEmits<{ pick: [tag: string]; close: [] }>()

const groups = ref<TagGroup[]>([])
const filter = ref('')
const box = ref<HTMLInputElement | null>(null)
const root = ref<HTMLElement | null>(null)

const BUILTIN_CATS = ['술식', '치식', '임플란트', '이식재', '차폐막', '기타']
// 서버가 아는 사용자 정의 카테고리 포함
const categories = computed(() => {
  const names = new Set(BUILTIN_CATS.slice(0, -1))
  groups.value.forEach((g) => names.add(g.name))
  names.delete('기타')
  return [...names, '기타']
})
const catMenu = ref<{ tag: string; count: number; x: number; y: number } | null>(null)

// 새 태그 추가 등으로 tag_groups가 밖에서 갱신되면 열려 있는 피커도 즉시 반영
watch(tagRanks, async () => {
  groups.value = await fetchTagGroups()
})

function openCatMenu(e: MouseEvent, tag: string) {
  e.preventDefault()
  const count = groups.value.flatMap((g) => g.tags).find((t) => t.tag === tag)?.count ?? 0
  const MENU_H = 390
  catMenu.value = {
    tag,
    count,
    x: Math.min(e.clientX, window.innerWidth - 240),
    y: Math.min(e.clientY, window.innerHeight - MENU_H),
  }
}

async function deleteTag() {
  const m = catMenu.value
  catMenu.value = null
  if (!m) return
  const detail = m.count
    ? `사용 중인 폴더 ${m.count}개에서도 제거되며 폴더명이 변경됩니다. (되돌리기 가능)`
    : '어휘 목록에서만 제거됩니다 (사용 중인 폴더 없음).'
  if (!window.confirm(`태그 "${m.tag}"를 정말 삭제할까요?\n${detail}`)) return
  try {
    const res = await fetch('/api/tag_delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tag: m.tag }),
    })
    if (!res.ok) throw new Error((await res.json()).detail ?? `HTTP ${res.status}`)
    groups.value = await fetchTagGroups(true)
    loadTagRanks(true).catch(() => {})
  } catch (e: any) {
    window.alert(`삭제 실패: ${e.message ?? e}`)
  }
}

function findTag(name: string) {
  return groups.value.flatMap((g) => g.tags).find((t) => t.tag === name)
}

async function callRename(oldTag: string, newTag: string) {
  const res = await fetch('/api/tag_rename', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag: oldTag, new: newTag }),
  })
  if (!res.ok) throw new Error((await res.json()).detail ?? `HTTP ${res.status}`)
  groups.value = await fetchTagGroups(true)
  loadTagRanks(true).catch(() => {})
}

async function renameTag() {
  const m = catMenu.value
  catMenu.value = null
  if (!m) return
  const input = window.prompt('새 태그 이름', m.tag)?.trim()
  if (!input || input === m.tag) return
  if (findTag(input) &&
      !window.confirm(`이미 있는 태그입니다. "${m.tag}"를 "${input}"에 합칠까요? (${m.count}개 폴더 반영)`)) return
  try {
    await callRename(m.tag, input)
  } catch (e: any) {
    window.alert(`이름 변경 실패: ${e.message ?? e}`)
  }
}

async function mergeTag() {
  const m = catMenu.value
  catMenu.value = null
  if (!m) return
  const input = window.prompt('합칠 대상 태그 이름')?.trim()
  if (!input || input === m.tag) return
  if (!findTag(input)) {
    window.alert(`"${input}" 태그가 없습니다`)
    return
  }
  if (!window.confirm(`${m.count}개 폴더에서 "${m.tag}"를 "${input}"로 합칩니다`)) return
  try {
    await callRename(m.tag, input)
  } catch (e: any) {
    window.alert(`합치기 실패: ${e.message ?? e}`)
  }
}

async function newCategory() {
  const name = window.prompt('새 카테고리 이름')?.trim()
  if (name) await assignCategory(name)
  else catMenu.value = null
}

async function assignCategory(cat: string) {
  const tag = catMenu.value?.tag
  catMenu.value = null
  if (!tag) return
  await fetch('/api/tag_category', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag, category: cat }),
  })
  groups.value = await fetchTagGroups(true) // 그룹 갱신
  loadTagRanks(true).catch(() => {})        // 카드 표기 순서도 갱신
}

const W = 420
const H = 380
// body로 Teleport + fixed — content-visibility 컨테인먼트/스크롤 클리핑 탈출.
// 아래 공간이 모자라면 버튼 위로 뒤집는다.
const style = computed(() => {
  const a = props.anchor
  const left = Math.max(8, Math.min(a.left, window.innerWidth - W - 12))
  const below = a.bottom + 6
  const top = below + H > window.innerHeight - 8 ? Math.max(8, a.top - H - 6) : below
  return { left: `${left}px`, top: `${top}px` }
})

const filtered = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return groups.value
  return groups.value
    .map((g) => ({ ...g, tags: g.tags.filter((t) => t.tag.toLowerCase().includes(q)) }))
    .filter((g) => g.tags.length)
})

function pick(tag: string) {
  if (props.existing.includes(tag)) return
  emit('pick', tag)
}

function commitFree() {
  const t = filter.value.trim()
  if (t) {
    emit('pick', t)
    filter.value = ''
  } else {
    emit('close')
  }
}

function onDocClick(e: MouseEvent) {
  if (root.value && !root.value.contains(e.target as Node)) emit('close')
}

onMounted(async () => {
  groups.value = await fetchTagGroups()
  box.value?.focus()
  setTimeout(() => document.addEventListener('mousedown', onDocClick), 0)
})
onUnmounted(() => document.removeEventListener('mousedown', onDocClick))
</script>

<template>
  <Teleport to="body">
  <div ref="root" class="tag-picker" :style="style" @click.stop>
    <input
      ref="box"
      v-model="filter"
      placeholder="필터 또는 새 태그 입력 후 Enter"
      @keydown.enter.prevent="commitFree"
      @keydown.escape.stop="emit('close')"
    />
    <div class="tp-groups">
      <div v-for="g in filtered" :key="g.name" class="tp-group">
        <div class="tp-head">
          <span class="tp-dot" :style="{ background: g.color }" />{{ g.name }}
        </div>
        <div class="tp-tags" :class="{ teeth: g.name === '치식' }">
          <button
            v-for="t in g.tags"
            :key="t.tag"
            class="tp-tag"
            :class="{ used: existing.includes(t.tag) }"
            :disabled="existing.includes(t.tag)"
            :title="(t.count ? `${t.count}개 폴더에서 사용 중` : '미사용 태그') + ' · 우클릭=카테고리 지정'"
            @click="pick(t.tag)"
            @contextmenu="openCatMenu($event, t.tag)"
          >{{ t.tag }}</button>
        </div>
      </div>
      <div v-if="!filtered.length" class="tp-empty">
        일치 없음 — Enter로 "{{ filter }}" 새 태그 추가
      </div>
    </div>
    <div
      v-if="catMenu"
      class="cat-menu"
      :style="{ left: catMenu.x + 'px', top: catMenu.y + 'px' }"
      @mouseleave="catMenu = null"
    >
      <div class="cat-menu-title">"{{ catMenu.tag }}" 카테고리</div>
      <button v-for="c in categories" :key="c" @click="assignCategory(c)">{{ c }}</button>
      <button @click="newCategory">➕ 새 카테고리…</button>
      <div class="cat-menu-sep" />
      <button @click="renameTag">✏ 이름 변경…</button>
      <button @click="mergeTag">⇄ 다른 태그에 합치기…</button>
      <div class="cat-menu-sep" />
      <button class="cat-menu-delete" @click="deleteTag">
        🗑 태그 삭제{{ catMenu.count ? ` (${catMenu.count}개 폴더 사용 중)` : '' }}
      </button>
    </div>
  </div>
  </Teleport>
</template>
