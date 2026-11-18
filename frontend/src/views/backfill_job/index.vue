<template>
  <section class="page" data-module="backfill_job">
    <header class="page-head">
      <div><h2>补数调度</h2><p class="page-desc">维护补数任务的输入、执行约束与结果记录；分批补数待补分区，中断恢复不覆盖已完成分区。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记补数任务</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
        <RouterLink class="btn" to="/backfill_result" data-testid="goto-results">补数分区结果集</RouterLink>
      </div>
    </header>
    <div class="filter-bar">
      <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
      <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
      <button class="btn" type="button" @click="reload">查询</button>
    </div>

    <div class="batch-bar" data-testid="batch-bar">
      <label style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" data-testid="select-all" :checked="allSelected" @change="toggleAll" /> 全选
      </label>
      <span>已选 {{ selectedIds.length }} 条，可一次提交整组任务：</span>
      <button
        v-for="action in actions"
        :key="action"
        class="btn"
        type="button"
        :data-testid="`batch-${action}`"
        :disabled="!selectedIds.length"
        @click="runBatch(action)"
      >
        批量{{ action }}
      </button>
      <span v-if="batchMessage" class="batch-message" data-testid="batch-message">{{ batchMessage }}</span>
    </div>

    <table class="data-table" style="margin-bottom:16px">
      <thead>
        <tr><th style="width:36px">选择</th><th>任务编号</th><th>状态</th><th>完成水位</th><th>失败区间</th><th>补数结果</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in filteredItems" :key="item.id" :data-testid="`job-row-${item.id}`" :data-status="item.status">
          <td><input type="checkbox" :value="item.id" v-model="selectedIds" :data-testid="`select-${item.id}`" /></td>
          <td>{{ item.name }}</td>
          <td>{{ item.status }}</td>
          <td>{{ watermarkOf(item.id) || '—' }}</td>
          <td>{{ failedOf(item.id) || '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`result-${item.id}`" @click="gotoResult(item.id)">回查结果集</button>
          </td>
        </tr>
      </tbody>
    </table>

    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
        <span class="timeline-label">{{ item.name || item.label }}</span>
        <div class="timeline-track">
          <div class="timeline-bar" :class="`status-${item.status}`" :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }" :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`" @click="selectItem(item)">&nbsp;</div>
        </div>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
    </div>
    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记补数任务</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { request } from '@/api/client'
const ENDPOINT = '/api/backfill_job'
const columns = ["任务编号", "目标表名", "起始日期", "截止日期", "并行度", "覆盖策略", "完成水位", "失败区间"]
const actions = ["启动任务", "确认完成", "取消任务"]
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])
const jobDetails = ref<Record<number, any>>({})
const selectedIds = ref<number[]>([])
const batchMessage = ref('')
const router = useRouter()
const filteredItems = computed(() => timelineItems.value)
const allSelected = computed(() => filteredItems.value.length > 0 && selectedIds.value.length === filteredItems.value.length)
function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
function toggleAll(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  selectedIds.value = checked ? filteredItems.value.map((i: any) => i.id) : []
}
function watermarkOf(id: number) { return jobDetails.value[id]?.['完成水位'] ?? '' }
function failedOf(id: number) { return jobDetails.value[id]?.['失败区间'] ?? '' }
async function reload() {
  try {
    const res = await request(ENDPOINT); const data = await res.json(); allItems.value = data.items || []; applyFilter()
    await loadDetails()
  } catch(e) { errorMessage.value = '加载失败' }
}
async function loadDetails() {
  const details: Record<number, any> = {}
  await Promise.all(allItems.value.map(async (item: any) => {
    try {
      const res = await request(`${ENDPOINT}/${item.id}`)
      if (res.ok) details[item.id] = await res.json()
    } catch { /* 详情失败不阻塞列表 */ }
  }))
  jobDetails.value = details
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items; selectedItem.value = null
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
async function runBatch(action: string) {
  batchMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/batch-actions`, {
      method: 'POST',
      body: JSON.stringify({ job_ids: selectedIds.value, action }),
    })
    if (!res.ok) throw new Error('批量操作失败')
    const payload = await res.json()
    const failed = (payload.items || []).filter((i: any) => !i.ok)
    batchMessage.value = failed.length
      ? `${payload.message}；失败：${failed.map((i: any) => `${i.任务id}(${i.message})`).join('，')}`
      : payload.message
    selectedIds.value = []
    await reload()
  } catch (e: any) {
    batchMessage.value = e.message
  }
}
async function gotoResult(id: number) {
  // 先操作补数调度列表，再到结果集详情中回查补数任务结果
  await router.push({ path: '/backfill_result', query: { job: String(id) } })
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(reload)
</script>
