<template>
  <section class="page" data-module="dwd_layer">
    <header class="page-head">
      <div>
        <h2>明细层管理管理</h2>
        <p class="page-desc">维护明细层DWD表的输入、执行约束与结果记录，为明细层管理提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记明细表</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出明细层管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`backfill-result-${row.id}`"
              @click="openBackfillResult(row)"
            >
              补数结果集
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无明细层管理数据，可先登记明细表</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记明细表</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="backfillResult" class="modal-backdrop" data-testid="backfill-modal" @click.self="backfillResult = null">
      <div class="modal" style="max-width:760px">
        <h3>补数分区结果集 · {{ backfillResult.目标表名 }} <small style="color:var(--muted)">v{{ backfillResult.版本号 }}</small></h3>
        <p>
          <strong>完成水位：</strong><span data-testid="backfill-watermark">{{ backfillResult.完成水位 || '—' }}</span>
          ｜ 完成分区 {{ backfillResult.完成分区.length }} 个
          ｜ 失败分区 {{ backfillResult.失败分区.length }} 个
        </p>
        <table class="data-table">
          <thead><tr><th>补数任务</th><th>覆盖窗口</th><th>状态</th><th>完成水位</th><th>失败分区</th></tr></thead>
          <tbody>
            <tr v-for="job in backfillJobList" :key="job.任务编号">
              <td>{{ job.任务编号 }}</td>
              <td>{{ job.起始日期 }} ~ {{ job.截止日期 }}</td>
              <td>{{ job.状态 }}</td>
              <td>{{ job.完成水位 || '—' }}</td>
              <td>{{ (job.失败分区 || []).join('、') || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="backfillResult.边界冲突?.length" class="error-text">
          存在 {{ backfillResult.边界冲突.length }} 处边界重叠冲突，生效版本：{{ backfillResult.边界冲突.map((c: any) => c.生效版本).join('、') }}
        </p>
        <div style="display:flex;justify-content:flex-end;margin-top:12px">
          <button class="btn primary" type="button" @click="backfillResult = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条明细层管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'
type Row = Record<string, string | number | null>

const ENDPOINT = '/api/dwd_layer'
const columns = ["表编号", "父层来源", "表名称", "粒度说明", "分区字段", "关联键列", "刷新水位", "创建批次"]
const actions = ["提交校验", "确认上线", "标记下线"]
const statuses = ["草稿", "待校验", "已上线", "已下线"]
const metricNames = ["明细表总数", "待校验表", "已上线表"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'dwd_layer.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('明细层管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '明细层管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('明细表列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '明细层管理列表读取失败'
  }
}

const backfillResult = ref<Record<string, any> | null>(null)
const backfillJobList = computed(() => Object.values(backfillResult.value?.任务结果 ?? {}) as Record<string, any>[])

async function openBackfillResult(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/backfill-result`)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '补数分区结果集读取失败')
    backfillResult.value = payload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补数分区结果集读取失败'
  }
}

onMounted(reload)
</script>
