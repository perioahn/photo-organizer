export interface FolderNode {
  id: number
  name: string
  date6: string | null
  is_regular: boolean
  tags: string[]
  image_count: number
  file_count: number
  cover: number | null
  children: FolderNode[]
}

export interface Patient {
  id: number
  folder_name: string
  num: string | null
  name: string
  folders: FolderNode[]
}

export interface FileEntry {
  id: number
  name: string
  size: number
  mtime_ns: number
  kind: 'image' | 'raw' | 'video'
}

export interface TagCount {
  tag: string
  count: number
}

export interface TagGroup {
  name: string
  color: string
  order: number
  tags: { tag: string; count: number }[]
}

let _tagGroups: Promise<TagGroup[]> | null = null
export function fetchTagGroups(force = false): Promise<TagGroup[]> {
  if (force || !_tagGroups) {
    _tagGroups = fetch('/api/tag_groups').then((r) => r.json())
  }
  return _tagGroups
}

// 태그 표기 순서: 서버 tag_groups의 그룹/그룹내 순서 그대로 (술식→치식→임플란트→…)
import { ref as vueRef } from 'vue'
export const tagRanks = vueRef<Map<string, number> | null>(null)

export async function loadTagRanks(force = false): Promise<void> {
  const groups = await fetchTagGroups(force)
  const m = new Map<string, number>()
  groups.forEach((g, gi) => g.tags.forEach((t, ti) => m.set(t.tag, gi * 1000 + ti)))
  tagRanks.value = m
}

export function sortTagsForDisplay(tags: string[]): string[] {
  const m = tagRanks.value
  if (!m) return tags
  const rank = (t: string) =>
    t.startsWith('@') ? -1 : m.get(t) ?? (t.startsWith('#') ? 1999 : 9999)
  return [...tags].sort((a, b) => rank(a) - rank(b))
}

export interface PdfHit {
  pdf_id: number
  page: number
  filename: string
  date8: string | null
  kind: string
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`)
  return res.json()
}

export const api = {
  tree: () => getJson<Patient[]>('/api/tree'),
  search: (q: string) => getJson<Patient[]>(`/api/search?q=${encodeURIComponent(q)}`),
  tags: () => getJson<TagCount[]>('/api/tags'),
  folderFiles: (id: number) => getJson<FileEntry[]>(`/api/folder/${id}/files`),
  health: () => getJson<Record<string, unknown>>('/api/health'),
  rescan: () => fetch('/api/rescan', { method: 'POST' }),
  openFolder: (id: number) => fetch(`/api/open_folder/${id}`, { method: 'POST' }),
  thumbUrl: (fileId: number) => `/api/thumb/${fileId}`,
  imageUrl: (fileId: number) => `/api/image/${fileId}`,
  patientPdfs: (num: string, date6?: string) =>
    getJson<PdfHit[]>(`/api/patient/${num}/pdfs${date6 ? `?date6=${date6}` : ''}`),
  pdfUrl: (pdfId: number, page: number) => `/api/pdf/${pdfId}#page=${page}`,
  editTags: async (folderId: number, add: string[], remove: string[]) => {
    const res = await fetch(`/api/folder/${folderId}/tags`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ add, remove }),
    })
    if (!res.ok) throw new Error((await res.json()).detail ?? `HTTP ${res.status}`)
    return res.json() as Promise<{ run_id: string | null; old: string; new: string }>
  },
  undo: async (runId: string) => {
    const res = await fetch(`/api/undo/${runId}`, { method: 'POST' })
    if (!res.ok) throw new Error((await res.json()).detail ?? `HTTP ${res.status}`)
    return res.json()
  },
}

export function connectEvents(handlers: Record<string, (data: any) => void>): EventSource {
  const es = new EventSource('/api/events')
  for (const [name, fn] of Object.entries(handlers)) {
    es.addEventListener(name, (e) => fn(JSON.parse((e as MessageEvent).data)))
  }
  return es
}
