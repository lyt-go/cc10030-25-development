<template>
  <section class="window-panel" data-testid="coord-window-panel">
    <header class="panel-head">
      <h3>格时窗口与样本资格</h3>
      <span class="panel-hint">空窗带原因；临时估算不计入正式统计。</span>
    </header>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>批次编号</span>
        <input v-model="batchKey" data-testid="window-batch" placeholder="输入采集批次编号" />
      </label>
      <button class="btn" type="submit" :disabled="!batchKey">读取窗口</button>
      <button class="btn" type="button" data-testid="window-build" :disabled="!batchKey" @click="build">
        登记窗口
      </button>
      <button class="btn" type="button" :disabled="!batchKey" @click="inspect">巡检领取</button>
      <button class="btn" type="button" :disabled="!batchKey" @click="claim('zero_only')">
        零窗部分领取
      </button>
      <button class="btn" type="button" :disabled="!batchKey" @click="claim('all')">正式领取</button>
    </form>

    <p v-if="message" class="panel-message" data-testid="window-message">{{ message }}</p>
    <p v-if="error" class="error-text">{{ error }}</p>

    <table v-if="windows.length" class="data-table">
      <thead>
        <tr>
          <th>窗口</th>
          <th>观测状态</th>
          <th>样本资格</th>
          <th>观测数</th>
          <th>临时估算</th>
          <th>正式统计</th>
          <th>空窗原因</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="cell in windows" :key="cell.window_id">
          <td>{{ cell.time_bucket_str }} · {{ cell.geohash ?? '未归格' }}</td>
          <td>{{ cell.observation_status }}</td>
          <td>{{ cell.sample_eligibility }}</td>
          <td>{{ cell.valid_count }}/{{ cell.observation_count }}</td>
          <td>{{ format(cell.provisional_estimate) }}</td>
          <td>{{ format(cell.formal_estimate) }}</td>
          <td>{{ cell.empty_reason || '—' }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else-if="loaded" class="empty-state">{{ emptyReason || '暂无格时窗口' }}</p>

    <div v-if="claims.length" class="claim-list">
      <h4>领取记录</h4>
      <ul>
        <li v-for="claim in claims" :key="claim.claim_id">
          <span>{{ claim.claim_id }}</span>
          <span>{{ claim.claim_kind }} · {{ claim.status }}</span>
          <span>{{ claim.delivery ?? '—' }}</span>
          <button class="link" type="button" @click="confirm(claim.claim_id)">确认</button>
          <button class="link" type="button" @click="replay(claim.claim_id)">复算</button>
        </li>
      </ul>
    </div>

    <div v-if="replayResult" class="replay-box" data-testid="window-replay">
      <p>
        复算 {{ replayResult.claim_id }}：原值 {{ format(replayResult.claimed_total) }}，
        复算 {{ format(replayResult.recomputed_total) }}，
        {{ replayResult.all_match ? '逐点一致' : '存在差异' }}
      </p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  buildWindows,
  claimFormal,
  claimInspection,
  confirmClaim,
  listClaims,
  listWindows,
  replayClaim,
  type ClaimSummary,
  type ReplayResult,
  type WindowCell,
} from '@/api/coord-windows'

const batchKey = ref('')
const windows = ref<WindowCell[]>([])
const claims = ref<ClaimSummary[]>([])
const emptyReason = ref('')
const loaded = ref(false)
const message = ref('')
const error = ref('')
const replayResult = ref<ReplayResult | null>(null)

function format(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : value.toFixed(6)
}

function report(cause: unknown, fallback: string) {
  error.value = cause instanceof Error ? cause.message : fallback
}

async function reload() {
  if (!batchKey.value) return
  error.value = ''
  message.value = ''
  try {
    const [windowPayload, claimPayload] = await Promise.all([
      listWindows(batchKey.value),
      listClaims(batchKey.value),
    ])
    windows.value = windowPayload.windows
    emptyReason.value = windowPayload.empty_reason
    claims.value = claimPayload.claims
    loaded.value = true
  } catch (cause) {
    report(cause, '窗口读取失败')
  }
}

async function build() {
  error.value = ''
  try {
    await buildWindows(batchKey.value)
    message.value = '窗口已登记，空窗会带上原因'
    await reload()
  } catch (cause) {
    report(cause, '窗口登记失败')
  }
}

async function inspect() {
  error.value = ''
  try {
    const claim = await claimInspection(batchKey.value)
    message.value = `巡检临时领取 ${claim.claim_id}，仅供复算，不计入正式统计`
    await reload()
  } catch (cause) {
    report(cause, '巡检领取失败')
  }
}

async function claim(scope: 'all' | 'zero_only') {
  error.value = ''
  try {
    const result = await claimFormal(batchKey.value, scope)
    message.value = `正式领取 ${result.claim_id}（${result.delivery}），审核确认后才产出正式值`
    await reload()
  } catch (cause) {
    report(cause, '正式领取失败')
  }
}

async function confirm(claimId: string) {
  error.value = ''
  try {
    const result = await confirmClaim(claimId)
    message.value = `已确认 ${claimId}，正式版本 v${result.formal_version}（${result.delivery}）`
    await reload()
  } catch (cause) {
    report(cause, '领取确认失败')
  }
}

async function replay(claimId: string) {
  error.value = ''
  try {
    replayResult.value = await replayClaim(claimId)
  } catch (cause) {
    report(cause, '领取复算失败')
  }
}
</script>
