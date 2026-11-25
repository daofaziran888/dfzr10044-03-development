<template>
  <section class="page" data-testid="evolve-detail">
    <header class="page-head">
      <div>
        <h2>分区演化交接详情</h2>
        <p class="page-desc">
          演化记录 {{ entryId }} · {{ detail?.evolve['演化编号'] ?? '加载中' }}
          · {{ detail?.evolve['所属湖表'] ?? '' }}
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/partition_evolve">返回演化清单</RouterLink>
        <button
          v-if="!detail?.handoff"
          class="btn primary"
          data-testid="open-handoff"
          type="button"
          :disabled="busy"
          @click="onOpenHandoff"
        >
          开立交接清单
        </button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="detail-error">{{ errorMessage }}</p>

    <template v-if="detail">
      <article class="stat-card" style="margin-bottom: 12px">
        <strong>演化状态：</strong>{{ detail.evolve.status }}
        ｜原分区键：{{ detail.evolve['原分区键'] }}
        ｜新分区键：{{ detail.evolve['新分区键'] }}
        ｜历史数据处置：{{ detail.evolve['历史数据处置'] }}
      </article>

      <div v-if="detail.progress" class="stat-row" data-testid="progress-row">
        <article class="stat-card">
          <span class="stat-label">已覆盖行数</span>
          <strong class="stat-value" data-testid="covered-rows">{{ detail.progress.covered_rows }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">仍欠行数</span>
          <strong class="stat-value" data-testid="outstanding-rows">{{ detail.progress.outstanding_rows }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">临时引用行数</span>
          <strong class="stat-value" data-testid="borrowed-rows">{{ detail.progress.borrowed_rows }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">重复到达次数</span>
          <strong class="stat-value" data-testid="duplicate-count">{{ detail.progress.duplicate_arrival_count }}</strong>
        </article>
      </div>

      <div v-if="detail.handoff" class="stat-card" style="margin: 12px 0">
        <p>
          <strong>交接状态：</strong>
          <span data-testid="handoff-status">{{ detail.handoff.status }}</span>
        </p>
        <p>
          基线快照：<code>{{ detail.handoff.baseline_snapshot_id }}</code>
          （旧快照时间旅行仍可读）
        </p>
        <p v-if="detail.handoff.confirmed_snapshot_id">
          确认快照：
          <code data-testid="confirmed-snapshot">{{ detail.handoff.confirmed_snapshot_id }}</code>
          ｜实际采用文件 {{ detail.handoff.adopted_file_ids.length }} 个
        </p>
      </div>

      <div v-if="detail.handoff" class="page-actions" style="margin: 12px 0; flex-wrap: wrap; gap: 8px">
        <button class="btn" data-testid="btn-migration" type="button" :disabled="busy" @click="openMigration">
          登记迁移产物
        </button>
        <button class="btn" data-testid="btn-borrowed" type="button" :disabled="busy" @click="openBorrowed">
          迟到文件临时引用
        </button>
        <button class="btn primary" data-testid="btn-confirm" type="button" :disabled="busy" @click="onConfirm">
          确认切换
        </button>
        <button class="btn" data-testid="btn-read-new" type="button" :disabled="busy" @click="onReadNew">
          沿新分区读取
        </button>
        <button class="btn" data-testid="btn-archive" type="button" :disabled="busy" @click="onArchive">
          归档旧目录
        </button>
        <button class="btn ghost" type="button" :disabled="busy" @click="reload">刷新</button>
      </div>

      <p v-if="readResult" class="stat-card" data-testid="read-result" style="margin: 12px 0">
        {{ readResult.message }}
        ｜可读：{{ readResult.readable ? '是' : '否' }}
        ｜行数：{{ readResult.rows }}
        <template v-if="readResult.snapshot_id">｜快照：<code>{{ readResult.snapshot_id }}</code></template>
      </p>

      <h3 v-if="detail.handoff" style="margin-top: 18px">分区覆盖进度</h3>
      <table v-if="detail.progress" class="data-table" data-testid="partition-table">
        <thead>
          <tr>
            <th>分区</th>
            <th>旧文件行数</th>
            <th>已覆盖行数</th>
            <th>临时引用行数</th>
            <th>仍欠行数</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in detail.progress.partitions" :key="row.partition">
            <td>{{ row.partition }}</td>
            <td>{{ row.old_rows }}</td>
            <td>{{ row.covered_rows }}</td>
            <td>{{ row.borrowed_rows }}</td>
            <td>{{ row.outstanding_rows }}</td>
            <td>{{ row.status }}</td>
          </tr>
          <tr v-if="!detail.progress.partitions.length">
            <td colspan="6" class="empty-state">暂无旧分区覆盖数据</td>
          </tr>
        </tbody>
      </table>

      <h3 style="margin-top: 18px">文件覆盖交接项</h3>
      <table class="data-table" data-testid="coverage-table">
        <thead>
          <tr>
            <th>旧文件</th>
            <th>旧分区</th>
            <th>旧行数</th>
            <th>新产物文件</th>
            <th>新行数</th>
            <th>交接批次</th>
            <th>到达次数</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in detail.handoff?.coverage ?? []" :key="item.source_file_id">
            <td><code>{{ item.source_file_id }}</code></td>
            <td>{{ item.source_partition }}</td>
            <td>{{ item.source_rows }}</td>
            <td><code>{{ item.new_file_id ?? '—' }}</code></td>
            <td>{{ item.new_rows }}</td>
            <td>{{ item.handoff_no ?? '—' }}</td>
            <td>{{ item.arrival_count }}</td>
            <td>{{ item.status }}</td>
          </tr>
          <tr v-if="!(detail.handoff?.coverage.length)">
            <td colspan="8" class="empty-state">尚未登记迁移产物</td>
          </tr>
        </tbody>
      </table>

      <h3 style="margin-top: 18px">迟到文件临时引用</h3>
      <table class="data-table" data-testid="borrowed-table">
        <thead>
          <tr>
            <th>原文件</th>
            <th>日分区</th>
            <th>行数</th>
            <th>处理批次</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in detail.handoff?.borrowed ?? []" :key="item.source_file_id">
            <td><code>{{ item.source_file_id }}</code></td>
            <td>{{ item.source_partition }}</td>
            <td>{{ item.rows }}</td>
            <td>{{ item.handoff_no ?? '待确认时落定' }}</td>
            <td>{{ item.reason }}</td>
          </tr>
          <tr v-if="!(detail.handoff?.borrowed.length)">
            <td colspan="5" class="empty-state">暂无临时引用文件</td>
          </tr>
        </tbody>
      </table>

      <h3 style="margin-top: 18px">交接状态流转</h3>
      <table class="data-table" data-testid="events-table">
        <thead>
          <tr>
            <th>序号</th>
            <th>交接批次</th>
            <th>动作</th>
            <th>说明</th>
            <th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="event in detail.handoff?.events ?? []" :key="event.seq">
            <td>{{ event.seq }}</td>
            <td>{{ event.handoff_no ?? '—' }}</td>
            <td>{{ event.action }}</td>
            <td>{{ event.detail }}</td>
            <td>{{ formatTime(event.at) }}</td>
          </tr>
          <tr v-if="!(detail.handoff?.events.length)">
            <td colspan="5" class="empty-state">暂无流转记录</td>
          </tr>
        </tbody>
      </table>
    </template>

    <div v-if="formVisible" class="modal-backdrop" data-testid="handoff-modal">
      <form class="modal" @submit.prevent="submitForm">
        <h3>{{ formMode === 'migration' ? '登记迁移产物' : '迟到文件临时引用' }}</h3>
        <template v-if="formMode === 'migration'">
          <label class="create-field">
            <span>旧文件 file_id</span>
            <input v-model="formSourceId" data-testid="form-source-id" required />
          </label>
          <label class="create-field">
            <span>新产物 file_id</span>
            <input v-model="formNewId" data-testid="form-new-id" required />
          </label>
        </template>
        <template v-else>
          <label class="create-field">
            <span>迟到旧文件 file_id</span>
            <input v-model="formSourceId" data-testid="form-source-id" required />
          </label>
        </template>
        <div class="page-actions">
          <button class="btn" type="button" @click="formVisible = false">取消</button>
          <button class="btn primary" data-testid="form-submit" type="submit" :disabled="busy">
            提交
          </button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  archiveOldDirectory,
  confirmSwitch,
  fetchEvolveDetail,
  openHandoff,
  readNewPartition,
  registerBorrowed,
  registerMigration,
  type EvolveDetail,
  type NewPartitionRead,
} from '@/api/handoff'

const route = useRoute()
const entryId = Number(route.params.id)
const detail = ref<EvolveDetail | null>(null)
const errorMessage = ref('')
const busy = ref(false)
const formVisible = ref(false)
const formMode = ref<'migration' | 'borrowed'>('migration')
const formSourceId = ref('')
const formNewId = ref('')
const readResult = ref<NewPartitionRead | null>(null)

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString()
}

async function reload() {
  errorMessage.value = ''
  try {
    detail.value = await fetchEvolveDetail(entryId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '演化详情读取失败'
  }
}

async function onOpenHandoff() {
  busy.value = true
  errorMessage.value = ''
  try {
    await openHandoff(entryId)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '开立交接清单失败'
  } finally {
    busy.value = false
  }
}

function openMigration() {
  formMode.value = 'migration'
  formSourceId.value = ''
  formNewId.value = ''
  formVisible.value = true
}

function openBorrowed() {
  formMode.value = 'borrowed'
  formSourceId.value = ''
  formNewId.value = ''
  formVisible.value = true
}

async function submitForm() {
  if (!detail.value?.handoff) return
  busy.value = true
  errorMessage.value = ''
  try {
    if (formMode.value === 'migration') {
      await registerMigration(detail.value.handoff.handoff_id, formSourceId.value.trim(), formNewId.value.trim())
    } else {
      await registerBorrowed(detail.value.handoff.handoff_id, formSourceId.value.trim())
    }
    formVisible.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交接登记失败'
  } finally {
    busy.value = false
  }
}

async function onConfirm() {
  if (!detail.value?.handoff) return
  busy.value = true
  errorMessage.value = ''
  try {
    await confirmSwitch(detail.value.handoff.handoff_id)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '确认切换失败'
  } finally {
    busy.value = false
  }
}

async function onReadNew() {
  if (!detail.value?.handoff) return
  busy.value = true
  errorMessage.value = ''
  try {
    readResult.value = await readNewPartition(detail.value.handoff.handoff_id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '新分区读取失败'
  } finally {
    busy.value = false
  }
}

async function onArchive() {
  if (!detail.value?.handoff) return
  busy.value = true
  errorMessage.value = ''
  try {
    await archiveOldDirectory(detail.value.handoff.handoff_id)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归档失败'
  } finally {
    busy.value = false
  }
}

onMounted(reload)
</script>
