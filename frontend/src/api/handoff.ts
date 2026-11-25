import { request } from './client'

export type CoverageRow = {
  source_file_id: string
  source_partition: string
  source_rows: number
  new_file_id: string | null
  new_rows: number
  handoff_no: number | null
  arrival_count: number
  status: string
  handled_at: number | null
}

export type BorrowedRow = {
  source_file_id: string
  source_partition: string
  rows: number
  reason: string
  handoff_no: number | null
}

export type HandoffEvent = {
  seq: number
  handoff_no: number | null
  action: string
  detail: string
  at: number
}

export type HandoffProgress = {
  handoff_id: string
  lake_table: string
  status: string
  covered_rows: number
  outstanding_rows: number
  borrowed_rows: number
  covered_partition_count: number
  outstanding_partitions: string[]
  old_active_rows: number
  new_product_rows: number
  duplicate_arrival_count: number
  partitions: {
    partition: string
    old_rows: number
    covered_rows: number
    outstanding_rows: number
    borrowed_rows: number
    status: string
  }[]
  readable_snapshot_id: string | null
}

export type HandoffDetail = {
  handoff_id: string
  evolve_id: number
  lake_table: string
  old_key: string
  new_key: string
  status: string
  baseline_snapshot_id: string
  confirmed_snapshot_id: string | null
  baseline_file_ids: string[]
  adopted_file_ids: string[]
  archived_file_ids: string[]
  retained_file_ids: string[]
  coverage: CoverageRow[]
  borrowed: BorrowedRow[]
  events: HandoffEvent[]
  created_at: number
  confirmed_at: number | null
}

export type EvolveDetail = {
  evolve: {
    id: number
    status: string
    [field: string]: string | number | boolean
  }
  handoff: HandoffDetail | null
  progress: HandoffProgress | null
}

async function postJson<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const data = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(data.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    const data = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(data.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export function fetchEvolveDetail(entryId: number): Promise<EvolveDetail> {
  return getJson(`/api/partition_evolve/${entryId}/detail`)
}

export function openHandoff(evolveId: number): Promise<HandoffDetail> {
  return postJson('/api/partition_evolve/handoffs', { evolve_id: evolveId })
}

export function registerMigration(
  handoffId: string,
  sourceFileId: string,
  newFileId: string,
): Promise<unknown> {
  return postJson(`/api/partition_evolve/handoffs/${handoffId}/migrations`, {
    source_file_id: sourceFileId,
    new_file_id: newFileId,
  })
}

export function registerBorrowed(
  handoffId: string,
  sourceFileId: string,
): Promise<unknown> {
  return postJson(`/api/partition_evolve/handoffs/${handoffId}/borrowed`, {
    source_file_id: sourceFileId,
  })
}

export function fetchProgress(handoffId: string): Promise<HandoffProgress> {
  return getJson(`/api/partition_evolve/handoffs/${handoffId}/progress`)
}

export function confirmSwitch(handoffId: string): Promise<{
  handoff_id: string
  status: string
  confirmed_snapshot_id: string
  baseline_snapshot_id: string
  adopted_files: { file_id: string; partition_key: string; rows: number; source_table: string }[]
  adopted_rows: number
}> {
  return postJson(`/api/partition_evolve/handoffs/${handoffId}/confirm`)
}

export type NewPartitionRead = {
  handoff_id: string
  status: string
  readable: boolean
  snapshot_id: string | null
  rows: number
  files: { file_id: string; partition_key: string; rows: number }[]
  message: string
}

export function readNewPartition(handoffId: string): Promise<NewPartitionRead> {
  return getJson(`/api/partition_evolve/handoffs/${handoffId}/read-new`)
}

export function archiveOldDirectory(handoffId: string): Promise<{
  handoff_id: string
  status: string
  archived_file_ids: string[]
  retained_file_ids: string[]
  baseline_snapshot_id: string
}> {
  return postJson(`/api/partition_evolve/handoffs/${handoffId}/archive`)
}
