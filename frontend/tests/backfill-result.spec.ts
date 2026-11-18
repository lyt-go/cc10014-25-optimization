import { expect, test, request as playwrightRequest } from '@playwright/test'

/**
 * 补数优化专项验收：
 * - 补数调度列表多选后整组批量提交，逐条返回结果
 * - 先操作补数调度列表，再在补数分区结果集详情中回查补数任务结果
 * - 结果集记录完成分区、失败分区与完成水位，同表只保留一个完整版本
 * - 明细层管理围绕结果集跨模块联动
 */
test.describe('补数调度优化与补数分区结果集', () => {
  test('批量补数后在结果集详情回查任务结果', async ({ page }) => {
    await page.goto('/backfill_job')
    await expect(page.locator('.timeline-bar').first()).toBeVisible({ timeout: 10000 })

    // 批量操作条与多选框
    const batchBar = page.getByTestId('batch-bar')
    await expect(batchBar).toBeVisible()
    const checkboxes = page.locator('tbody input[type="checkbox"]')
    expect(await checkboxes.count()).toBeGreaterThanOrEqual(2)

    await checkboxes.nth(0).check()
    await checkboxes.nth(1).check()
    await expect(page.getByTestId('batch-message')).toHaveCount(0)
    await page.getByTestId('batch-启动任务').click()

    // 一次提交整组任务，逐条结果汇总成一条批次消息
    const message = page.getByTestId('batch-message')
    await expect(message).toBeVisible()
    expect(await message.innerText()).toContain('成功 2/2')

    // 先操作补数调度列表，随后跳转结果集详情回查该任务结果
    const firstRow = page.locator('[data-testid^="job-row-"]').first()
    await firstRow.getByRole('button', { name: '回查结果集' }).click()
    await page.waitForURL(/\/backfill_result\?job=\d+/)
    await expect(page.getByTestId('detail-modal')).toBeVisible()
    await expect(page.getByTestId('detail-watermark')).toBeVisible()
    const watermark = await page.getByTestId('detail-watermark').innerText()
    expect(watermark).toMatch(/\d{4}-\d{2}-\d{2}/)

    // 结果集列表：每张表只有一个完整版本
    await page.locator('.modal .btn.primary').click()
    const resultRows = page.locator('tbody [data-testid^="row-"]')
    expect(await resultRows.count()).toBeGreaterThanOrEqual(1)
    const tableNames = await resultRows.evaluateAll((nodes) =>
      nodes.map((n) => n.getAttribute('data-testid')),
    )
    expect(new Set(tableNames).size).toBe(tableNames.length)
  })

  test('明细层管理回查补数分区结果集', async ({ page }) => {
    const api = await playwrightRequest.newContext({ baseURL: 'http://127.0.0.1:5173' })
    // 先通过接口准备一条针对 dwd_order_detail 的补数任务并执行
    const create = await api.post('/api/backfill_job', {
      data: {
        values: {
          任务编号: 'BF-E2E-DWD-001',
          目标表名: 'dwd_order_detail',
          起始日期: '2026-09-10',
          截止日期: '2026-09-12',
          并行度: '4',
          覆盖策略: '覆盖写入',
          完成水位: '',
          失败区间: '',
        },
      },
    })
    expect(create.ok()).toBeTruthy()
    const created = await create.json()
    const jobId = created.entry.id
    const run = await api.post(`/api/backfill_job/${jobId}/actions`, { data: { action: '启动任务' } })
    expect(run.ok()).toBeTruthy()
    const runPayload = await run.json()
    expect(runPayload.entry['完成水位']).toBe('2026-09-12')

    // DWD 列表第一条（dwd_order_detail）回查同一份结果集
    await page.goto('/dwd_layer')
    const firstRow = page.locator('tbody tr[data-testid^="row-"]').first()
    await firstRow.getByRole('button', { name: '补数结果集' }).click()
    const modal = page.getByTestId('backfill-modal')
    await expect(modal).toBeVisible()
    await expect(page.getByTestId('backfill-watermark')).toHaveText('2026-09-12')
    await expect(modal.getByText('BF-E2E-DWD-001')).toBeVisible()
    await api.dispose()
  })

  test('相邻任务重叠边界：结果集保留候选版本、差异记录与唯一生效版本', async ({ page }) => {
    const api = await playwrightRequest.newContext({ baseURL: 'http://127.0.0.1:5173' })
    const table = 'ods_e2e_overlap'
    for (const [no, start, end] of [
      ['BF-E2E-OV-001', '2026-08-01', '2026-08-05'],
      ['BF-E2E-OV-002', '2026-08-05', '2026-08-08'],
    ] as const) {
      const create = await api.post('/api/backfill_job', {
        data: {
          values: {
            任务编号: no, 目标表名: table, 起始日期: start, 截止日期: end,
            并行度: '4', 覆盖策略: '覆盖写入', 完成水位: '', 失败区间: '',
          },
        },
      })
      const payload = await create.json()
      const started = await api.post(`/api/backfill_job/${payload.entry.id}/actions`, {
        data: { action: '启动任务' },
      })
      expect(started.ok()).toBeTruthy()
    }

    await page.goto('/backfill_result')
    const row = page.getByTestId(`row-${table}`)
    await expect(row).toBeVisible()
    await row.getByRole('button', { name: '查看详情' }).click()
    const modal = page.getByTestId('detail-modal')
    // 两个候选版本、差异记录、唯一生效版本
    const conflict = modal.getByTestId('conflict-box')
    await expect(conflict).toBeVisible()
    await expect(conflict.getByText('BF-E2E-OV-001', { exact: false }).first()).toBeVisible()
    await expect(conflict.getByText('BF-E2E-OV-002', { exact: false }).first()).toBeVisible()
    await expect(conflict.getByText('唯一生效版本：BF-E2E-OV-001')).toBeVisible()
    // 单一边界版本与合并任务号
    const merged = modal.getByTestId('merged-view')
    await expect(merged).toContainText('single')
    await expect(merged).toContainText('BF-E2E-OV-001,BF-E2E-OV-002')
    await api.dispose()
  })
})
