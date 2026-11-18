<template>
  <section class="page" data-module="backfill_result">
    <header class="page-head">
      <div>
        <h2>补数分区结果集</h2>
        <p class="page-desc">按目标表保存补数分区的唯一完整版本：完成分区、失败分区与完成水位同步落库，并发写入只保留一个完整版本。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="refresh" @click="reload">刷新结果集</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card"><span class="stat-label">结果集（表）</span><strong class="stat-value" data-testid="stat-tables">{{ items.length }}</strong></article>
      <article class="stat-card"><span class="stat-label">完成分区总数</span><strong class="stat-value" data-testid="stat-done">{{ totalCompleted }}</strong></article>
      <article class="stat-card"><span class="stat-label">失败分区总数</span><strong class="stat-value" data-testid="stat-failed">{{ totalFailed }}</strong></article>
      <article class="stat-card"><span class="stat-label">边界冲突表</span><strong class="stat-value" data-testid="stat-conflict">{{ conflictTables }}</strong></article>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>目标表名</th>
          <th>分区字段</th>
          <th>完成水位</th>
          <th>完成分区</th>
          <th>失败分区</th>
          <th>待补分区</th>
          <th>版本号</th>
          <th>更新时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in items" :key="row.目标表名" :data-testid="`row-${row.目标表名}`">
          <td>{{ row.目标表名 }}</td>
          <td>{{ row.分区字段 }}</td>
          <td data-testid="watermark">{{ row.完成水位 || '—' }}</td>
          <td>{{ row.完成分区.length }}</td>
          <td :class="{ 'error-text': row.失败分区.length }">{{ row.失败分区.length }}</td>
          <td>{{ (row.待补分区 || []).length }}</td>
          <td>v{{ row.版本号 }}</td>
          <td>{{ row.更新时间 }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`detail-${row.目标表名}`" @click="openDetail(row.目标表名)">查看详情</button>
          </td>
        </tr>
        <tr v-if="!items.length">
          <td colspan="9" class="empty-state">暂无补数分区结果集，请先在补数调度中执行补数任务</td>
        </tr>
      </tbody>
    </table>

    <div v-if="detail" class="modal-backdrop" data-testid="detail-modal" @click.self="detail = null">
      <div class="modal" style="max-width:860px">
        <h3>结果集详情 · {{ detail.目标表名 }} <small style="color:var(--muted)">v{{ detail.版本号 }}</small></h3>

        <p><strong>完成水位：</strong><span data-testid="detail-watermark">{{ detail.完成水位 || '—' }}</span>
          ｜ <strong>分区字段：</strong>{{ detail.分区字段 }}</p>

        <h4 style="margin:10px 0 4px">补数任务结果（沿数据流回查）</h4>
        <table class="data-table">
          <thead><tr><th>任务编号</th><th>覆盖窗口</th><th>状态</th><th>完成</th><th>失败</th><th>完成水位</th></tr></thead>
          <tbody>
            <tr v-for="job in jobList" :key="job.任务编号" :data-testid="`job-${job.任务编号}`">
              <td>{{ job.任务编号 }}</td>
              <td>{{ job.起始日期 }} ~ {{ job.截止日期 }}</td>
              <td>{{ job.状态 }}</td>
              <td>{{ job.完成分区.length }}</td>
              <td>{{ job.失败分区.length }}</td>
              <td>{{ job.完成水位 || '—' }}</td>
            </tr>
          </tbody>
        </table>

        <template v-if="detail.边界冲突 && detail.边界冲突.length">
          <h4 style="margin:10px 0 4px">边界冲突（候选版本 / 差异记录 / 生效版本）</h4>
          <div v-for="(conflict, idx) in detail.边界冲突" :key="idx" class="conflict-box" data-testid="conflict-box">
            <p>
              <strong>重叠分区：</strong>{{ conflict.重叠分区.join('、') }}<br />
              <strong>候选版本：</strong>
              <span v-for="c in conflict.候选版本" :key="c.任务编号">{{ c.任务编号 }}（{{ c.覆盖窗口 }}） </span><br />
              <strong>唯一生效版本：</strong><span class="error-text">{{ conflict.生效版本 }}</span><br />
              <strong>判定依据：</strong>{{ conflict.判定依据 }}
            </p>
            <details>
              <summary>差异记录（{{ conflict.差异记录.length }}）</summary>
              <ul>
                <li v-for="d in conflict.差异记录" :key="d.分区">
                  分区 {{ d.分区 }}：候选 {{ d.候选任务.join(' / ') }} → 生效 {{ d.生效任务 }}（{{ d.差异说明 }}）
                </li>
              </ul>
            </details>
          </div>
          <h4 style="margin:10px 0 4px">合并视图（单一边界版本 + 合并任务号）</h4>
          <p data-testid="merged-view">
            边界版本：{{ detail.合并视图.边界版本 }} ｜ 合并任务号：{{ detail.合并视图.合并任务号 }}
          </p>
        </template>

        <h4 style="margin:10px 0 4px">完成分区（{{ detail.完成分区.length }}）</h4>
        <p class="partition-tags">
          <span v-for="p in detail.完成分区" :key="p" class="tag ok">{{ p }}</span>
        </p>
        <h4 style="margin:10px 0 4px">失败分区（{{ detail.失败分区.length }}）</h4>
        <p class="partition-tags">
          <span v-for="p in detail.失败分区" :key="p" class="tag bad">{{ p }}</span>
          <span v-if="!detail.失败分区.length" style="color:var(--muted)">无</span>
        </p>

        <div style="display:flex;justify-content:flex-end;margin-top:12px">
          <button class="btn primary" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { request } from '@/api/client'

const ENDPOINT = '/api/backfill_job/results'
type ResultRow = Record<string, any>
const items = ref<ResultRow[]>([])
const detail = ref<ResultRow | null>(null)
const errorMessage = ref('')
const route = useRoute()

const totalCompleted = computed(() => items.value.reduce((sum, r) => sum + (r.完成分区?.length ?? 0), 0))
const totalFailed = computed(() => items.value.reduce((sum, r) => sum + (r.失败分区?.length ?? 0), 0))
const conflictTables = computed(() => items.value.filter((r) => (r.边界冲突?.length ?? 0) > 0).length)
const jobList = computed(() => Object.values(detail.value?.任务结果 ?? {}) as ResultRow[])

async function reload() {
  errorMessage.value = ''
  try {
    const res = await request(ENDPOINT)
    if (!res.ok) throw new Error('结果集读取失败')
    const data = await res.json()
    items.value = data.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结果集读取失败'
  }
}

async function openDetail(tableName: string) {
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/${encodeURIComponent(tableName)}`)
    if (!res.ok) throw new Error('结果集详情读取失败')
    detail.value = await res.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结果集详情读取失败'
  }
}

onMounted(async () => {
  await reload()
  // 从补数调度列表跳转过来：沿数据流直接回查该任务的补数分区结果集详情
  const jobId = route.query.job
  if (jobId) {
    try {
      const res = await request(`/api/backfill_job/${jobId}/result`)
      if (res.ok) detail.value = await res.json()
    } catch {
      /* 任务尚未产生结果集时停留列表页 */
    }
  }
})
</script>

<style scoped>
.conflict-box {
  border: 1px solid var(--border, #d9d9d9);
  border-radius: 6px;
  padding: 8px 12px;
  margin: 6px 0;
  background: var(--surface-soft, #fafafa);
}
.partition-tags { display: flex; flex-wrap: wrap; gap: 6px; max-height: 140px; overflow: auto; }
.tag { padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.tag.ok { background: #e6f7e6; color: #1a7f37; }
.tag.bad { background: #fde8e8; color: #c0392b; }
</style>
