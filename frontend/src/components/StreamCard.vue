<script setup lang="ts">
import { computed, ref } from 'vue'
import { api, ApiError, offerLabel, type LinkView, type Offer, type PushResult, type StreamView } from '../api'
import ConfirmDialog from './ConfirmDialog.vue'
import OfferSearch from './OfferSearch.vue'

const props = defineProps<{ stream: StreamView; pushAllowed: boolean }>()
const emit = defineEmits<{ update: [StreamView] }>()

const busy = ref(false)
const error = ref('')
const notice = ref('')
const newOffer = ref<Offer | null>(null)
const pinEdit = ref<{ offerId: number; value: number } | null>(null)
const confirm = ref<'cancel' | 'push' | null>(null)

const s = computed(() => props.stream)
const locked = computed(() => busy.value || s.value.push_state === 'pushing' || s.value.remote_missing)
const sideEffect = computed(() => s.value.side_effect_fields?.length > 0)
// Результат прошлого Push неизвестен: правки и Cancel заблокированы, доступны повтор Push (сверка) и Fetch.
const reconcile = computed(() => !!s.value.reconciliation_required)
const editLocked = computed(() => locked.value || reconcile.value)

const STATE_LABEL: Record<string, string> = {
  published: '', changed: 'изменён', new: 'новый', restoring: 'возвращается', pending_remove: 'будет удалён', archived: 'архив',
}

// Каждый ответ несёт draft_version; более старый снимок не перезаписывает новый.
function apply(next: StreamView) {
  if (next.draft_version >= s.value.draft_version) emit('update', next)
}

async function run(action: () => Promise<StreamView | PushResult>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const res = await action()
    if ('stream' in res) {
      apply(res.stream)
      // side_effect_detected — не успех: показывается постоянным красным блоком из stream.side_effect_fields.
      if (res.result !== 'side_effect_detected') {
        notice.value = res.result === 'published' ? 'Опубликовано и подтверждено чтением из Keitaro'
          : res.result === 'already_applied' ? 'Изменения уже были в Keitaro — подтверждено чтением' : 'Нечего публиковать'
      }
    } else apply(res)
  } catch (e) {
    if (e instanceof ApiError) {
      error.value = e.message
      if (e.body.stream) apply(e.body.stream)
    } else error.value = 'Неизвестная ошибка'
  } finally {
    busy.value = false
  }
}

const add = () => newOffer.value && run(async () => {
  const r = await api.addOffer(s.value, newOffer.value!.id)
  newOffer.value = null
  return r
})
const op = (l: LinkView, what: 'remove' | 'restore' | 'unpin') => run(() => api.linkOp(s.value, l.offer_id, what))
const savePin = () => pinEdit.value && run(async () => {
  const r = await api.linkOp(s.value, pinEdit.value!.offerId, 'pin', pinEdit.value!.value)
  pinEdit.value = null
  return r
})
const doConfirm = () => {
  const what = confirm.value
  confirm.value = null
  if (what === 'cancel') run(() => api.cancel(s.value))
  if (what === 'push') run(() => api.push(s.value))
}
</script>

<template>
  <article class="card stream" :class="{ dirty: s.dirty, missing: s.remote_missing }" :data-stream="s.id">
    <header class="row between">
      <div>
        <h3>#{{ s.position }} {{ s.name }} <span class="muted small">ID {{ s.id }}</span></h3>
        <span v-if="s.dirty" class="badge warn">не опубликовано</span>
        <span v-if="s.push_state === 'pushing'" class="badge">публикация…</span>
        <span v-if="s.remote_missing" class="badge danger">удалён в Keitaro</span>
      </div>
      <div v-if="s.has_offers" class="row">
        <button type="button" :disabled="editLocked || !s.dirty"
          :title="reconcile ? 'Недоступно, пока результат прошлого Push не сверен' : ''" @click="confirm = 'cancel'">Cancel</button>
        <button type="button" class="primary" :disabled="locked || !s.dirty || !pushAllowed || sideEffect"
          :title="sideEffect ? 'Push заблокирован: изменились защищённые поля потока' : pushAllowed ? '' : 'Публикация разрешена только для своих тестовых кампаний'"
          @click="confirm = 'push'">{{ reconcile ? 'Повторить Push (сверка)' : 'Push to KT' }}</button>
      </div>
    </header>

    <p v-if="!s.has_offers" class="muted">Поток без офферов — редактор его не изменяет.</p>
    <template v-else>
      <table class="offers">
        <thead><tr><th>Оффер</th><th class="num">Доля</th><th>Pin</th><th></th></tr></thead>
        <tbody>
          <tr v-for="l in s.links" :key="l.offer_id" :class="[l.status, l.view_state]" :data-offer="l.offer_id">
            <td>
              <b>{{ l.offer_id }}</b> {{ offerLabel(l.offer_id, l.offer_name) || '(нет в каталоге)' }}
              <span v-if="STATE_LABEL[l.view_state]" class="tag" :class="l.view_state">{{ STATE_LABEL[l.view_state] }}</span>
              <span v-if="l.kt_state === 'disabled'" class="tag">выключен в Keitaro</span>
              <span v-if="l.pub_present && l.share !== l.pub_share && l.status === 'active'" class="muted small"> (было {{ l.pub_share }}%)</span>
            </td>
            <td class="num">
              <div class="share"><span :style="{ width: l.share + '%' }"></span></div>
              <b class="share-value">{{ l.share }}%</b>
            </td>
            <td>
              <template v-if="l.status === 'active' && l.kt_state === 'active'">
                <form v-if="pinEdit?.offerId === l.offer_id" class="row pin-form" @submit.prevent="savePin">
                  <input v-model.number="pinEdit.value" type="number" min="1" max="100" aria-label="Закреплённая доля" />
                  <button type="submit" :disabled="editLocked">OK</button>
                  <button type="button" @click="pinEdit = null">×</button>
                </form>
                <template v-else>
                  <button v-if="l.pinned" type="button" class="pin on" :disabled="editLocked" title="Открепить" @click="op(l, 'unpin')">📌 {{ l.share }}%</button>
                  <button v-else type="button" class="pin" :disabled="editLocked" title="Закрепить долю" @click="pinEdit = { offerId: l.offer_id, value: l.share }">pin</button>
                </template>
              </template>
            </td>
            <td class="actions">
              <button v-if="l.status === 'active'" type="button" :disabled="editLocked" @click="op(l, 'remove')">Remove</button>
              <button v-else type="button" :disabled="editLocked" @click="op(l, 'restore')">Bring back</button>
            </td>
          </tr>
        </tbody>
        <tfoot>
          <tr><td>Сумма активных</td><td class="num" :class="{ error: s.active_sum !== 100 }"><b>{{ s.active_sum }}%</b></td><td colspan="2"></td></tr>
        </tfoot>
      </table>

      <form class="row add" @submit.prevent="add">
        <OfferSearch v-model="newOffer" :disabled="editLocked" placeholder="Добавить оффер: ID или название" />
        <button type="submit" :disabled="editLocked || !newOffer">Добавить</button>
      </form>
    </template>

    <div v-if="sideEffect" class="alert error side-effect" role="alert" data-testid="side-effect">
      <b>СТОП: после Push в Keitaro изменились защищённые поля потока: {{ s.side_effect_fields.join(', ') }}.</b>
      Офферы опубликованы и подтверждены чтением, но прогон нужно остановить и проверить поток в Keitaro.
      Push этого потока заблокирован до Fetch.
    </div>
    <div v-if="reconcile" class="alert warn reconcile" role="alert" data-testid="reconcile">
      <b>Результат предыдущего Push неизвестен.</b> Сначала нажмите «Повторить Push (сверка)» — он прочитает поток
      в Keitaro и повторно не запишет уже применённое — или сделайте Fetch. До этого добавление, Remove,
      Bring back, Pin и Cancel заблокированы.
    </div>
    <div v-if="error" class="alert error" role="alert">{{ error }}</div>
    <div v-else-if="s.last_error && !sideEffect" class="alert warn">{{ s.last_error }}</div>
    <div v-if="notice" class="alert ok">{{ notice }}</div>

    <ConfirmDialog v-if="confirm === 'cancel'" title="Отменить изменения?" danger confirm-label="Отменить изменения"
      text="Черновик потока вернётся к опубликованному состоянию. Новые неопубликованные связи будут удалены, сами офферы останутся."
      @confirm="doConfirm" @cancel="confirm = null" />
    <ConfirmDialog v-if="confirm === 'push'" title="Опубликовать в Keitaro?" confirm-label="Push"
      :text="`Состав офферов потока ${s.id} будет заменён в Keitaro: ${s.links.filter(l => l.status === 'active').map(l => l.offer_id + ' — ' + l.share + '%').join(', ')}.`"
      @confirm="doConfirm" @cancel="confirm = null" />
  </article>
</template>
