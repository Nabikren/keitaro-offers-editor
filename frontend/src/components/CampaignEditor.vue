<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { api, ApiError, type CampaignView, type StreamView } from '../api'
import ConfirmDialog from './ConfirmDialog.vue'
import StreamCard from './StreamCard.vue'

const props = defineProps<{ initialId?: number | null }>()

const campaignId = ref<number | null>(props.initialId ?? null)
const campaign = ref<CampaignView | null>(null)
const local = ref<{ id: number; name: string }[]>([])
const loading = ref(false)
const error = ref('')
const report = ref('')
const dirtyConflict = ref<number[] | null>(null)

watch(() => props.initialId, (id) => { if (id) { campaignId.value = id; fetchStreams() } })

async function loadLocal() {
  try { local.value = await api.localCampaigns() } catch { /* список не критичен */ }
}

async function open(id: number) {
  campaignId.value = id
  error.value = ''
  report.value = ''
  try { campaign.value = await api.campaign(id) }
  catch (e) {
    campaign.value = null
    if (e instanceof ApiError && e.status === 404) await fetchStreams()
    else error.value = e instanceof ApiError ? e.message : 'Ошибка'
  }
}

async function fetchStreams(discard = false) {
  if (!campaignId.value || loading.value) return
  loading.value = true
  error.value = ''
  report.value = ''
  try {
    const res = await api.fetchStreams(campaignId.value, discard)
    campaign.value = res.campaign
    const r = res.report as { removed_externally?: Record<string, number[]>; added?: Record<string, number[]> }
    const removed = Object.values(r.removed_externally ?? {}).flat()
    const added = Object.values(r.added ?? {}).flat()
    report.value = [
      added.length ? `новые из Keitaro: ${added.join(', ')}` : '',
      removed.length ? `удалены в Keitaro (перенесены в архив): ${removed.join(', ')}` : '',
    ].filter(Boolean).join('; ') || 'Потоки загружены'
    await loadLocal()
  } catch (e) {
    if (e instanceof ApiError && e.body.code === 'dirty_drafts') dirtyConflict.value = e.body.streams ?? []
    else {
      error.value = e instanceof ApiError ? e.message : 'Ошибка'
      if (e instanceof ApiError && e.body.campaign) campaign.value = e.body.campaign // актуальное состояние при конфликте
    }
  } finally {
    loading.value = false
  }
}

function replaceStream(next: StreamView) {
  if (!campaign.value) return
  campaign.value.streams = campaign.value.streams.map((s) => (s.id === next.id ? next : s))
}

onMounted(async () => {
  await loadLocal()
  if (campaignId.value) await open(campaignId.value)
})
</script>

<template>
  <section>
    <div class="card row toolbar">
      <label for="e-id">ID кампании</label>
      <input id="e-id" v-model.number="campaignId" type="number" min="1" class="id-input" @keydown.enter="fetchStreams()" />
      <button type="button" class="primary" :disabled="!campaignId || loading" @click="fetchStreams()">
        {{ loading ? 'Загрузка…' : 'Fetch streams from KT' }}
      </button>
      <select v-if="local.length" aria-label="Загруженные кампании" @change="open(Number(($event.target as HTMLSelectElement).value))">
        <option value="">Ранее загруженные…</option>
        <option v-for="c in local" :key="c.id" :value="c.id">{{ c.id }} — {{ c.name }}</option>
      </select>
    </div>

    <div v-if="error" class="alert error" role="alert">{{ error }}</div>
    <div v-if="report" class="alert ok small">{{ report }}</div>

    <template v-if="campaign">
      <h2>{{ campaign.name }} <span class="muted small">ID {{ campaign.id }}</span></h2>
      <div v-if="!campaign.push_allowed" class="alert warn small">
        Кампания не из собственного тестового набора: просмотр и черновик доступны, Push заблокирован.
      </div>
      <p v-if="!campaign.streams.length" class="muted">В кампании нет потоков.</p>
      <StreamCard v-for="s in campaign.streams" :key="s.id" :stream="s" :push-allowed="campaign.push_allowed" @update="replaceStream" />
    </template>
    <p v-else-if="!loading && !error" class="muted">Введите ID кампании и нажмите Fetch streams.</p>

    <ConfirmDialog v-if="dirtyConflict" title="Есть неопубликованные изменения" danger confirm-label="Заменить данными Keitaro"
      :text="`В потоках ${dirtyConflict.join(', ')} есть черновик. Fetch заменит его текущим состоянием Keitaro (архив связей сохранится).`"
      @confirm="dirtyConflict = null; fetchStreams(true)" @cancel="dirtyConflict = null" />
  </section>
</template>
