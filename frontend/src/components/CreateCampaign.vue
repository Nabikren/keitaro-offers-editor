<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api, ApiError, type AppConfig, type Job, type Offer, type References } from '../api'
import OfferSearch from './OfferSearch.vue'

const props = defineProps<{ config: AppConfig }>()
const emit = defineEmits<{ open: [campaignId: number] }>()

const form = reactive({ name: '', geo: '', domain_id: 0, group_id: 0, traffic_source_id: 0 })
const offer = ref<Offer | null>(null)
const refs = ref<References | null>(null)
const refsError = ref('')
const submitting = ref(false)
const error = ref('')
const job = ref<Job | null>(null)
// Ключ идемпотентности одной попытки: повторная отправка тех же данных не создаст вторую кампанию.
const key = ref(crypto.randomUUID())

const geoCodes = computed(() => form.geo.split(/[,\s;]+/).map((c) => c.trim().toUpperCase()).filter(Boolean))
const geoValid = computed(() => geoCodes.value.length > 0 && geoCodes.value.every((c) => /^[A-Z]{2}$/.test(c)))
const nameValid = computed(() => /^[\w\-. ]{1,120}$/u.test(form.name.trim().replace(props.config.campaign_name_prefix, '')))
const canSubmit = computed(() => !submitting.value && nameValid.value && geoValid.value && !!offer.value
  && !!form.domain_id && !!form.group_id && !!form.traffic_source_id)

watch([() => ({ ...form }), offer], () => {
  if (job.value && job.value.status !== 'running') { job.value = null; key.value = crypto.randomUUID() }
}, { deep: true })

onMounted(async () => {
  try {
    refs.value = await api.references()
    const d = props.config.defaults
    const pick = (list: { id: number }[], preferred: number | null) =>
      (preferred && list.some((x) => x.id === preferred) ? preferred : list.length === 1 ? list[0]!.id : 0)
    form.domain_id = pick(refs.value.domains, d.domain_id)
    form.group_id = pick(refs.value.groups, d.group_id)
    form.traffic_source_id = pick(refs.value.traffic_sources, d.traffic_source_id)
  } catch (e) {
    refsError.value = e instanceof ApiError ? e.message : 'Не удалось загрузить справочники'
  }
})

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = ''
  try {
    job.value = await api.createCampaign({ idempotency_key: key.value, name: form.name, geo: form.geo, offer_id: offer.value!.id,
      domain_id: form.domain_id, group_id: form.group_id, traffic_source_id: form.traffic_source_id })
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Ошибка'
  } finally {
    submitting.value = false
  }
}

async function resume() {
  if (!job.value) return
  submitting.value = true
  error.value = ''
  try { job.value = await api.resumeJob(job.value.idempotency_key) }
  catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Ошибка'
    if (e instanceof ApiError && e.body.job) job.value = e.body.job
  }
  finally { submitting.value = false }
}
</script>

<template>
  <section class="card narrow">
    <h2>Новая кампания</h2>
    <p class="muted small">
      Поток 1 принимает выбранные страны → <code>{{ config.redirect_url }}</code>. Поток 2 отправляет остальной трафик на оффер.
    </p>
    <form class="grid-form" @submit.prevent="submit">
      <label for="c-name">Название</label>
      <div>
        <div class="prefixed"><span>{{ config.campaign_name_prefix }}</span>
          <input id="c-name" v-model="form.name" maxlength="120" placeholder="mx-test-1" /></div>
        <div v-if="form.name && !nameValid" class="error small">Буквы, цифры, пробел, _ - . (до 120)</div>
      </div>

      <label for="c-geo">GEO</label>
      <div>
        <input id="c-geo" v-model="form.geo" placeholder="AU или MX,AU,RO" />
        <div class="small" :class="form.geo && !geoValid ? 'error' : 'muted'">
          Коды ISO 3166-1 alpha-2. Обязательно минимум одна страна; несколько через запятую — расширение.
        </div>
      </div>

      <label for="c-offer">Оффер</label>
      <OfferSearch v-model="offer" input-id="c-offer" />

      <template v-if="refs">
        <label for="c-domain">Домен</label>
        <select id="c-domain" v-model.number="form.domain_id">
          <option :value="0" disabled>— выберите —</option>
          <option v-for="d in refs.domains" :key="d.id" :value="d.id">{{ d.name }} ({{ d.id }})</option>
        </select>
        <label for="c-group">Группа</label>
        <select id="c-group" v-model.number="form.group_id">
          <option :value="0" disabled>— выберите —</option>
          <option v-for="g in refs.groups" :key="g.id" :value="g.id">{{ g.name }} ({{ g.id }})</option>
        </select>
        <label for="c-source">Источник</label>
        <select id="c-source" v-model.number="form.traffic_source_id">
          <option :value="0" disabled>— выберите —</option>
          <option v-for="t in refs.traffic_sources" :key="t.id" :value="t.id">{{ t.name }} ({{ t.id }})</option>
        </select>
      </template>
      <template v-else>
        <span></span><div :class="refsError ? 'error' : 'muted'">{{ refsError || 'Загрузка доменов, групп и источников…' }}</div>
      </template>

      <span></span>
      <div class="row">
        <button class="primary" type="submit" :disabled="!canSubmit">{{ submitting ? 'Создание…' : 'Create' }}</button>
      </div>
    </form>

    <div v-if="error" class="alert error">{{ error }}</div>
    <div v-if="job" class="alert" :class="job.status === 'done' ? 'ok' : 'warn'" data-testid="job-result">
      <template v-if="job.status === 'done'">
        Кампания <b>{{ job.name }}</b> создана (ID {{ job.campaign_id }}), потоки {{ job.geo_stream_id }} → {{ job.offer_stream_id }} проверены чтением.
        <button type="button" @click="emit('open', job.campaign_id!)">Открыть в редакторе</button>
      </template>
      <template v-else-if="(job.status === 'running' || job.status === 'pending') && !job.stale">Создание выполняется…</template>
      <template v-else>
        <b>{{ job.stale ? 'Выполнение прервано' : job.status === 'partial' ? 'Создано частично' : 'Не создано' }}:</b> {{ job.last_error }}
        <div class="small">Кампания: {{ job.campaign_id ?? '—' }}, поток GEO: {{ job.geo_stream_id ?? '—' }}, поток оффера: {{ job.offer_stream_id ?? '—' }}.
          Повтор сначала сверяет уже созданное и не создаёт дублей.</div>
        <button type="button" :disabled="submitting" @click="resume">Продолжить</button>
      </template>
    </div>
  </section>
</template>
