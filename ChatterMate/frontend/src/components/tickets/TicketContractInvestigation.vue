<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import api from '@/services/api'

type Evidence = { id: string; source: string; line: number | null; record: string | null; text: string }
type Step = { id: string; question: string; status: string; claim: string | null; references: string[] }
type Job = {
  id: string
  contract_id: string
  billing_period: string
  status: 'queued' | 'running' | 'clarifying' | 'awaiting_input' | 'failed' | 'completed'
  analysis_request?: { question: string }
  published_at: string | null
  error: string | null
  snapshot: {
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
const emit = defineEmits<{ published: [] }>()
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
const reply = ref('')
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
  conflicting: '证据冲突', blocked: '等待前置步骤', pending: '待执行',
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

async function submit(path: string, form?: FormData): Promise<boolean> {
  if (busy.value) return false
  revision += 1
  busy.value = true
  error.value = ''
  try {
    const { data } = await api.post<Job>(
      `${url.value}${path}`, form,
      form ? { headers: { 'Content-Type': 'multipart/form-data' } } : undefined,
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
  if (job.value && await submit(`/${job.value.id}/publish`)) emit('published')
}

function openNewRun() {
  contract.value = job.value?.contract_id ?? ''
  period.value = job.value?.billing_period ?? ''
  mode.value = job.value?.analysis_request ? 'analysis' : 'contract'
  question.value = ''
  requestKey = null
  newRun.value = true
}
</script>

<template>
  <section v-if="enabled || job" class="contract-investigation" aria-label="合同付款调查">
    <div class="contract-heading">
      <h2>工单分析</h2>
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
      <ul v-if="job.snapshot.steps?.length" class="contract-steps">
        <li v-for="step in job.snapshot.steps" :key="step.id">
          <strong>{{ step.question }}</strong>
          <span class="contract-step-state">{{ stepLabels[step.status] ?? step.status }}</span>
          <p v-if="step.claim">{{ step.claim }} <small v-if="step.references.length">[{{ step.references.join(', ') }}]</small></p>
        </li>
      </ul>
      <details v-if="job.snapshot.evidence?.length">
        <summary>查看原文和台账引用</summary>
        <div v-for="item in job.snapshot.evidence" :key="item.id" class="contract-evidence">
          <strong>[{{ item.id }}] {{ item.source }}</strong>
          <span v-if="item.line !== null"> · 第 {{ item.line }} 行</span>
          <span v-else-if="item.record"> · {{ item.record }}</span>
          <p>{{ item.text }}</p>
        </div>
      </details>
      <form v-if="job.status === 'awaiting_input' && enabled && canManage" class="contract-form" @submit.prevent="sendSupplement">
        <p>{{ job.snapshot.prompt }}</p>
        <label>补充文件 <input type="file" accept=".txt,.md" required @change="selectFile($event, true)" /></label>
        <button type="submit" :disabled="busy || !supplement">补证并继续</button>
      </form>
      <div class="contract-actions">
        <button v-if="job.status === 'awaiting_input' && enabled && canManage" :disabled="busy"
          @click="submit(`/${job.id}/finish`)">用已有证据结束</button>
        <button v-if="job.status === 'failed' && enabled && canManage" :disabled="busy"
          @click="submit(`/${job.id}/retry`)">从失败步骤续跑</button>
        <button v-if="canPublish && canApprove && !job.published_at" :disabled="busy"
          @click="publish">确认并写入工单</button>
        <span v-if="job.published_at">已写入工单活动</span>
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
.contract-steps { padding-inline-start: 20px; }
.contract-steps li, .contract-evidence { padding-block: 6px; font-size: .875rem; }
.contract-evidence p { white-space: pre-wrap; overflow-wrap: anywhere; }
.contract-actions { margin-top: 12px; }
</style>
