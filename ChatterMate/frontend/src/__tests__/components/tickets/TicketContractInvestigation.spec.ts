import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import TicketContractInvestigation from '@/components/tickets/TicketContractInvestigation.vue'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('@/services/api', () => ({ default: api }))

const props = { ticketId: 'ticket-one', canManage: true, canApprove: true, settled: false }
const run = {
  id: 'run-one', contract_id: 'MT-2026-001', billing_period: '2026', published_at: null, error: null,
  snapshot: { active_step: 's2', next_nodes: ['retrieve_step'], steps: [
    { id: 's1', question: '合同年费', claim: '12,000 元', status: 'supported', references: ['L3'] },
    { id: 's2', question: '付款台账', claim: null, status: 'pending', references: [] },
  ] },
}

afterEach(() => vi.resetAllMocks())

describe('ticket contract investigations', () => {
  it('retries the original run from the ticket page', async () => {
    api.get.mockResolvedValue({ data: { enabled: true, job: { ...run, status: 'failed' } } })
    api.post.mockResolvedValue({ data: { ...run, status: 'queued' } })
    const wrapper = mount(TicketContractInvestigation, { props })
    try {
      await flushPromises()
      expect(wrapper.text()).toMatch(/付款台账\s*（retrieve_step）/)
      expect(wrapper.text()).toContain('12,000 元')
      await wrapper.get('button').trigger('click')
      await flushPromises()
      expect(api.post.mock.calls[0]?.[0]).toBe('/tickets/ticket-one/contract-investigation/run-one/retry')
      expect(wrapper.text()).toContain('等待执行')
    } finally {
      wrapper.unmount()
    }
  })

  it('shows evidence and reloads activity after a verified result is published', async () => {
    const done = {
      ...run, status: 'completed', report_version: 'version-one',
      snapshot: { ...run.snapshot, review_status: 'supported', balance: { amount: '7000.00', currency: 'CNY' },
        evidence: [{ id: 'L3', source: 'contract.txt', line: 3, record: null, text: '合同年费原文' }] },
    }
    api.get.mockResolvedValue({ data: { enabled: true, job: done } })
    api.post.mockResolvedValue({ data: { ...done, published_at: '2026-11-17T12:00:00Z' } })
    const wrapper = mount(TicketContractInvestigation, { props })
    try {
      await flushPromises()
      await wrapper.get('.reference-button').trigger('click')
      expect(wrapper.get('details').attributes('open')).toBeDefined()
      expect(wrapper.get('details').text()).toContain('合同年费原文')
      const publish = wrapper.findAll('button').find(button => button.text() === '确认并写入工单')
      await publish?.trigger('click')
      await flushPromises()
      expect(api.post.mock.calls[0]?.[2]).toEqual({ headers: { 'X-Report-Version': 'version-one' } })
      expect(wrapper.emitted('published')).toHaveLength(1)
      expect(wrapper.text()).toContain('已写入工单活动')
    } finally {
      wrapper.unmount()
    }
  })
})
