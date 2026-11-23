<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import api from '@/services/api'

type Evidence = { id: string; source: string; line: number | null; record: string | null; text: string }
type Step = { id: string; question: string; status: string; claim: string | null; references: string[]; depends_on?: string[]; material_revision?: number | null; review_note?: string | null; previous_claim?: string | null }
type Job = {
  id: string
  contract_id: string
  billing_period: string
  status: 'queued' | 'running' | 'clarifying' | 'awaiting_input' | 'failed' | 'completed'
  analysis_request?: { question: string; project?: string; hybrid?: boolean }
  report_version?: string | null
  published_at: string | null
  error: string | null
  snapshot: {
    document_revision?: number
    review_status?: string
    prompt?: string
    clarification?: { prompt: string; scope: string | null; directions: string[] }
    confirmed_scope?: string
    can_finish?: boolean
    next_nodes?: string[]
    active_step?: string
    steps?: Step[]
    evidence?: Evidence[]
    balance?: { amount: string; currency: string }
  }
}

const props = defineProps<{ ticketId: string; canManage: boolean; canApprove: boolean; settled: boolean; initialQuestion?: string }>()
const emit = defineEmits<{ published: []; availability: [enabled: boolean] }>()
const url = computed(() => `/tickets/${props.ticketId}/contract-investigation`)
const job = ref<Job | null>(null)
const enabled = ref(false)
const busy = ref(false)
const error = ref('')
const newRun = ref(false)
const contract = ref('')
const period = ref('')
const mode = ref<'contract' | 'analysis'>(props.initialQuestion ? 'analysis' : 'contract')
const question = ref(props.initialQuestion?.slice(0, 4000) ?? '')
const web = ref(false)
const configuredSources = ref(false)
const project = ref('')
const hybrid = ref(false)
const downloading = ref(false)
const reply = ref('')
const selectedEvidence = ref<{ runId: string; stepId: string }>()
const focusedStep = computed(() => selectedEvidence.value?.runId === job.value?.id
  ? job.value?.snapshot.steps?.find(step => step.id === selectedEvidence.value?.stepId) : undefined)
const visibleEvidence = computed(() => {
  const items = job.value?.snapshot.evidence ?? []
  return focusedStep.value ? items.filter(item => focusedStep.value?.references.includes(item.id)) : items
})
const verifiedCount = computed(() => job.value?.snapshot.steps?.filter(step => step.status === 'supported').length ?? 0)
const evidencePanel = ref<HTMLDetailsElement>()

function showEvidence(step: Step) {
  if (job.value) selectedEvidence.value = { runId: job.value.id, stepId: step.id }
  if (evidencePanel.value) evidencePanel.value.open = true
}
const document = ref<File>()
const supplement = ref<File>()
let requestKey: string | null = null
let poll: ReturnType<typeof setInterval> | undefined
let reading = false
let disposed = false
let loaded = false
let revision = 0

const statusLabels: Record<Job['status'], string> = {
  queued: '等待执行',
  running: '正在调查',
  clarifying: '等待确认分析范围',
  awaiting_input: '等待补充材料',
  failed: '调查中断，可续跑',
  completed: '调查完成',
}
const stepLabels: Record<string, string> = {
  supported: '有证据', inference: '有依据的推断', insufficient: '证据不足',
  partial: '部分核实', conflicting: '证据冲突', blocked: '等待前置步骤', pending: '待执行',
}
const currentStep = computed(() => job.value?.snapshot.steps?.find(
  step => step.id === job.value?.snapshot.active_step,
))
const canPublish = computed(() => job.value?.status === 'completed'
  && (!!job.value.analysis_request || (job.value.snapshot.review_status === 'supported' && !!job.value.snapshot.balance)))

function messageOf(cause: unknown): string {
  const detail = (cause as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' ? detail : '请求失败。刷新后可查看已保存的调查。'
}

async function refresh() {
  if (reading || busy.value) return
  reading = true
  const observedRevision = revision
  try {
    const { data } = await api.get<{ enabled: boolean; job: Job | null }>(url.value)
    if (!disposed && !busy.value && observedRevision === revision) {
      loaded = true
      enabled.value = data.enabled
      emit('availability', data.enabled)
      job.value = data.job
    }
  } catch {
    // Keep the last saved state visible while the connection recovers.
  } finally {
    reading = false
  }
}

onMounted(() => {
  void refresh()
  poll = setInterval(() => {
    if (!loaded || enabled.value) void refresh()
  }, 3000)
})
onUnmounted(() => {
  disposed = true
  if (poll) clearInterval(poll)
})

async function submit(path: string, form?: FormData, reportVersion?: string): Promise<boolean> {
  if (busy.value) return false
  revision += 1
  busy.value = true
  error.value = ''
  try {
    const { data } = await api.post<Job>(
      `${url.value}${path}`, form,
            form ? { headers: { 'Content-Type': 'multipart/form-data' } }
        : reportVersion ? { headers: { 'X-Report-Version': reportVersion } } : undefined,
    )
    if (!disposed) job.value = data
    return true
  } catch (cause) {
    error.value = messageOf(cause)
    return false
  } finally {
    busy.value = false
  }
}

function selectFile(event: Event, isSupplement: boolean) {
  const selected = (event.target as HTMLInputElement).files?.[0]
  if (isSupplement) {
    supplement.value = selected
  } else {
    document.value = selected
    requestKey = null
  }
}

async function start() {
  if (mode.value === 'contract' && !document.value) return
  const form = new FormData()
  requestKey ??= crypto.randomUUID()
  form.append('request_key', requestKey)
  if (mode.value === 'analysis') {
    form.append('question', question.value.trim())
    form.append('web_enabled', String(web.value))
    form.append('configured_sources', String(configuredSources.value))
    if (project.value.trim()) form.append('project', project.value.trim())
    if (hybrid.value) form.append('hybrid', 'true')
  } else {
    form.append('contract_id', contract.value.trim())
    form.append('billing_period', period.value.trim())
  }
  if (document.value) form.append('file', document.value)
  if (await submit(mode.value === 'analysis' ? '/analysis' : '', form)) {
    document.value = undefined
    newRun.value = false
    requestKey = null
  }
}

async function clarify(confirm: boolean) {
  if (!job.value) return
  const form = new FormData()
  form.append('confirm', String(confirm))
  if (!confirm) form.append('answer', reply.value.trim())
  if (await submit(`/${job.value.id}/clarification`, form)) reply.value = ''
}

async function sendSupplement() {
  if (!job.value || !supplement.value) return
  const form = new FormData()
  form.append('file', supplement.value)
  if (await submit(`/${job.value.id}/supplement`, form)) supplement.value = undefined
}

async function publish() {
  if (job.value?.report_version && await submit(`/${job.value.id}/publish`, undefined, job.value.report_version)) {
    emit('published')
  }
}

async function downloadReport() {
  if (!job.value || downloading.value) return
  const runId = job.value.id
  downloading.value = true
  error.value = ''
  try {
    const { data } = await api.get<Blob>(`${url.value}/${runId}/report`, { responseType: 'blob' })
    if (disposed) return
    const href = URL.createObjectURL(data)
    const link = window.document.createElement('a')
    link.href = href
    link.download = `analysis-${runId.replaceAll('-', '')}.md`
    link.click()
    setTimeout(() => URL.revokeObjectURL(href), 1000)
  } catch {
    error.value = '暂时无法下载报告，请重试。'
  } finally {
    downloading.value = false
  }
}

function openNewRun() {
  contract.value = job.value?.contract_id ?? ''
  period.value = job.value?.billing_period ?? ''
  mode.value = job.value?.analysis_request ? 'analysis' : 'contract'
  project.value = job.value?.analysis_request?.project ?? ''
  hybrid.value = job.value?.analysis_request?.hybrid ?? false
  question.value = ''
  requestKey = null
  newRun.value = true
}
</script>

<template>
  <section v-if="enabled || job" class="contract-investigation" aria-label="合同付款调查">
    <div class="contract-heading">
      <h2>工单分析 · Analysis Agent</h2>
      <span v-if="job" class="contract-state">{{ statusLabels[job.status] }}</span>
    </div>
    <p v-if="error || job?.error" role="status" class="contract-error">{{ error || job?.error }}</p>
    <form v-if="enabled && canManage && !settled && (!job || newRun)" class="contract-form" @submit.prevent="start">
      <label>分析方式
        <select v-model="mode" @change="requestKey = null">
          <option value="contract">合同付款核对</option>
          <option value="analysis">通用分析</option>
        </select>
      </label>
      <template v-if="mode === 'contract'">
        <label>合同编号 <input v-model.trim="contract" required maxlength="40" @input="requestKey = null" /></label>
        <label>计费周期 <input v-model.trim="period" required maxlength="40" @input="requestKey = null" /></label>
      </template>
      <template v-else>
        <label>分析需求 <textarea v-model.trim="question" required maxlength="4000" @input="requestKey = null" /></label>
        <label>项目记忆（选填）
          <input v-model.trim="project" maxlength="128" placeholder="输入项目名，留空则关闭记忆" @input="requestKey = null" />
        </label>
        <label>检索方式
          <select v-model="hybrid" @change="requestKey = null">
            <option :value="false">关键词检索</option>
            <option :value="true">混合检索（关键词 + 向量）</option>
          </select>
        </label>
        <label class="source-option"><input v-model="web" type="checkbox" @change="requestKey = null" /> 允许检索公开网页</label>
        <label class="source-option"><input v-model="configuredSources" type="checkbox" @change="requestKey = null" /> 使用管理员配置的资料目录和只读台账</label>
      </template>
      <label>{{ mode === 'contract' ? '合同文件' : '参考文件（可选）' }}（UTF-8 TXT 或 Markdown）
        <input type="file" accept=".txt,.md" :required="mode === 'contract'" @change="selectFile($event, false)" />
      </label>
      <button type="submit" :disabled="busy || (mode === 'contract' ? !document : !question)">开始调查</button>
    </form>
    <template v-if="job && !newRun">
      <p class="contract-meta">{{ job.analysis_request?.question || `${job.contract_id} · ${job.billing_period}` }} · 调查编号 {{ job.id }}</p>
      <p v-if="job.analysis_request?.project">项目记忆 · {{ job.analysis_request.project }}</p>
      <p v-if="job.analysis_request?.hybrid">检索方式 · 混合检索</p>
      <p v-if="job.snapshot.confirmed_scope">分析范围 · {{ job.snapshot.confirmed_scope }}</p>
      <form v-if="job.status === 'clarifying' && job.snapshot.clarification && enabled && canManage"
        class="contract-form" @submit.prevent="clarify(false)">
        <p>{{ job.snapshot.clarification.prompt }}</p>
        <p v-if="job.snapshot.clarification.scope">拟定范围 · {{ job.snapshot.clarification.scope }}</p>
        <button v-for="direction in job.snapshot.clarification.directions" :key="direction" type="button"
          @click="reply = direction">{{ direction }}</button>
        <label>补充信息 <textarea v-model="reply" maxlength="4000" /></label>
        <button type="submit" :disabled="busy || !reply.trim()">提交回答</button>
        <button v-if="job.snapshot.clarification.scope" type="button" :disabled="busy"
          @click="clarify(true)">确认范围并执行</button>
      </form>
      <p v-if="job.status === 'failed' && currentStep">
        恢复位置 · {{ currentStep.question }} <span v-if="job.snapshot.next_nodes?.length">（{{ job.snapshot.next_nodes.join('、') }}）</span>
      </p>
      <dl v-if="job.snapshot.steps?.length" class="investigation-summary">
        <div><dt>已核实</dt><dd>{{ verifiedCount }} / {{ job.snapshot.steps.length }} 项</dd></div>
        <div v-if="job.snapshot.balance"><dt>未付余额</dt><dd>{{ job.snapshot.balance.currency }} {{ job.snapshot.balance.amount }}</dd></div>
        <div v-if="job.snapshot.document_revision != null"><dt>材料版本</dt><dd>第 {{ job.snapshot.document_revision + 1 }} 版</dd></div>
      </dl>
      <div v-if="job.snapshot.steps?.length" class="investigation-workspace">
      <div>
        <h3>调查结论</h3>
        <ul class="contract-steps">
        <li v-for="step in job.snapshot.steps" :key="step.id">
          <strong>{{ step.question }}</strong>
          <span class="contract-step-state" :data-state="step.status">{{ stepLabels[step.status] ?? step.status }}</span>
          <p v-if="step.claim">{{ step.claim }}</p>
          <p v-if="step.previous_claim && step.previous_claim !== step.claim" class="muted">
            补证前 · {{ step.previous_claim }}
          </p>
          <p v-if="step.review_note" class="muted">{{ step.review_note }}</p>
          <p v-if="step.material_revision != null" class="muted">
            第 {{ step.material_revision + 1 }} 版材料核对
            <span v-if="step.material_revision < (job.snapshot.document_revision ?? 0)"> · 沿用原结果</span>
          </p>
          <button v-if="step.references.length && job.snapshot.evidence?.length" type="button"
            class="reference-button" :aria-pressed="focusedStep?.id === step.id" @click="showEvidence(step)">
            查看证据（{{ step.references.length }}）
          </button>
        </li>
        </ul>
      </div>
      <details v-if="job.snapshot.evidence?.length" ref="evidencePanel" class="evidence-panel">
        <summary>原文和台账引用</summary>
        <p v-if="focusedStep">
          {{ focusedStep.question }}
          <button type="button" class="reference-button" @click="selectedEvidence = undefined">显示全部</button>
        </p>
        <p v-if="!visibleEvidence.length" class="muted">本次快照没有保存该结论的引文。</p>
        <div v-for="item in visibleEvidence" :key="item.id" class="contract-evidence">
          <strong>[{{ item.id }}] {{ item.source }}</strong>
          <span v-if="item.line !== null"> · 第 {{ item.line }} 行</span>
          <span v-else-if="item.record"> · {{ item.record }}</span>
          <p>{{ item.text }}</p>
        </div>
      </details>
      </div>
      <form v-if="job.status === 'awaiting_input' && enabled && canManage" class="contract-form" @submit.prevent="sendSupplement">
        <p>{{ job.snapshot.prompt }}</p>
        <label>补充文件 <input type="file" accept=".txt,.md" required @change="selectFile($event, true)" /></label>
        <button type="submit" :disabled="busy || !supplement">补证并继续</button>
      </form>
      <details v-if="job.snapshot.steps?.length" class="execution-panel">
        <summary>执行记录</summary>
        <ol>
          <li v-for="step in job.snapshot.steps" :key="step.id">
            {{ step.question }} · {{ stepLabels[step.status] ?? step.status }}
            <span v-if="step.material_revision != null"> · 材料第 {{ step.material_revision + 1 }} 版</span>
            <span v-if="step.depends_on?.length">
              · 前置：{{ step.depends_on.map(id => job?.snapshot.steps?.find(item => item.id === id)?.question ?? id).join('、') }}
            </span>
          </li>
        </ol>
      </details>
      <div class="contract-actions">
        <button v-if="job.status === 'awaiting_input' && enabled && canManage" :disabled="busy"
          @click="submit(`/${job.id}/finish`)">用已有证据结束</button>
        <button v-if="job.status === 'failed' && enabled && canManage" :disabled="busy"
          @click="submit(`/${job.id}/retry`)">从失败步骤续跑</button>
        <button v-if="canPublish && canApprove && job.report_version && !job.published_at" :disabled="busy"
          @click="publish">确认并写入工单</button>
        <span v-if="job.published_at">已写入工单活动</span>
        <small v-if="job.report_version" class="muted">报告版本 {{ job.report_version.slice(0, 12) }}</small>
        <button v-if="job.status === 'completed' && enabled" type="button" :disabled="downloading"
          @click="downloadReport">{{ downloading ? '正在下载…' : '下载 Markdown 报告' }}</button>
        <button v-if="job.status === 'completed' && enabled && canManage && !settled" :disabled="busy"
          @click="openNewRun">发起新一轮{{ job.analysis_request ? '分析' : '核对' }}</button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.contract-investigation { border: 1px solid #dfe4e9; border-radius: 10px; padding: 16px; }
.contract-heading, .contract-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.contract-heading { justify-content: space-between; }
h2 { font-size: 1rem; font-weight: 650; margin: 0; }
.contract-state, .contract-step-state { font-size: .8rem; padding: 3px 8px; border-radius: 5px; background: #eaf1f8; color: #29476b; }
.contract-step-state { margin-left: 8px; white-space: nowrap; }
.contract-step-state[data-state="supported"] { background: var(--accent-bg-12); color: var(--accent-ink); }
.contract-step-state[data-state="insufficient"], .contract-step-state[data-state="conflicting"] {
  background: color-mix(in srgb, #cf8526 20%, transparent); color: inherit;
}
.contract-meta { overflow-wrap: anywhere; color: #566270; font-size: .8rem; }
.contract-form { display: grid; gap: 10px; margin-top: 12px; }
.contract-form label { display: grid; gap: 4px; font-size: .875rem; }
.contract-form input:not([type="checkbox"]), .contract-form textarea, .contract-form select {
  padding: 7px;
  border: 1px solid #bfc8d4;
  border-radius: 5px;
  width: min(100%, 380px);
}
.contract-form textarea { min-height: 5.5rem; resize: vertical; }
.contract-form .source-option { display: flex; align-items: center; gap: 8px; }
button { justify-self: start; border-radius: 6px; padding: 7px 12px; color: white; background: #314e70; }
button:disabled { opacity: .55; cursor: default; }
.contract-error { color: #a82f24; }
.investigation-summary { display: flex; flex-wrap: wrap; gap: 16px 32px; padding: 14px; border: 1px solid var(--border-color); border-radius: 8px; }
.investigation-summary dt, .muted { font-size: .8rem; opacity: .78; }
.investigation-summary dd { margin: 4px 0 0; font-weight: 650; }
.investigation-workspace { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, .8fr); gap: 16px; align-items: start; }
.investigation-workspace h3 { font-size: .9rem; margin: 0 0 8px; }
.contract-steps { padding: 0; margin: 0; list-style: none; }
.contract-steps li, .evidence-panel { border: 1px solid var(--border-color); border-radius: 8px; padding: 12px; background: var(--surface); }
.contract-steps li + li { margin-top: 8px; }
.contract-steps p { margin: 6px 0; }
.evidence-panel summary, .execution-panel summary { font-weight: 600; cursor: pointer; }
.execution-panel { margin-top: 14px; font-size: .875rem; }
.execution-panel li { margin-block: 5px; }
.reference-button { padding: 4px 8px; color: var(--accent-ink); background: var(--accent-bg-08); }
@media (max-width: 850px) {
  .investigation-workspace { grid-template-columns: minmax(0, 1fr); }
}
.contract-steps li, .contract-evidence { padding-block: 6px; font-size: .875rem; }
.contract-evidence p { white-space: pre-wrap; overflow-wrap: anywhere; }
.contract-actions { margin-top: 12px; }
</style>
