<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { api, ApiError, offerLabel, type Offer } from '../api'

const props = defineProps<{ modelValue: Offer | null; placeholder?: string; disabled?: boolean; inputId?: string }>()
const emit = defineEmits<{ 'update:modelValue': [Offer | null] }>()

const query = ref(props.modelValue ? `${props.modelValue.id} ${offerLabel(props.modelValue.id, props.modelValue.name)}` : '')
const results = ref<Offer[]>([])
const open = ref(false)
const loading = ref(false)
const error = ref('')
const highlighted = ref(0)
let timer: ReturnType<typeof setTimeout> | undefined
let controller: AbortController | undefined
let seq = 0

watch(() => props.modelValue, (v) => {
  if (!v) query.value = ''
})

function onInput() {
  emit('update:modelValue', null)
  clearTimeout(timer)
  timer = setTimeout(search, 250)
}

async function search() {
  const q = query.value.trim()
  controller?.abort()
  controller = new AbortController()
  const mySeq = ++seq
  loading.value = true
  error.value = ''
  try {
    const found = await api.searchOffers(q, controller.signal)
    if (mySeq !== seq) return // устаревший ответ не перезаписывает более новый поиск
    results.value = found
    highlighted.value = 0
    open.value = true
  } catch (e) {
    if ((e as Error).name === 'AbortError' || mySeq !== seq) return
    error.value = e instanceof ApiError ? e.message : 'Ошибка поиска'
  } finally {
    if (mySeq === seq) loading.value = false
  }
}

function choose(o: Offer) {
  // Отменяем отложенный/текущий поиск, иначе его ответ снова откроет список.
  clearTimeout(timer)
  controller?.abort()
  seq++
  loading.value = false
  emit('update:modelValue', o)
  query.value = `${o.id} ${offerLabel(o.id, o.name)}`
  open.value = false
}

function onKey(e: KeyboardEvent) {
  if (!open.value || !results.value.length) return
  if (e.key === 'ArrowDown') { highlighted.value = (highlighted.value + 1) % results.value.length; e.preventDefault() }
  else if (e.key === 'ArrowUp') { highlighted.value = (highlighted.value - 1 + results.value.length) % results.value.length; e.preventDefault() }
  else if (e.key === 'Enter') { choose(results.value[highlighted.value]!); e.preventDefault() }
  else if (e.key === 'Escape') open.value = false
}

onBeforeUnmount(() => { clearTimeout(timer); controller?.abort() })
</script>

<template>
  <div class="search" @focusout="open = false">
    <input
      :id="inputId"
      v-model="query"
      :placeholder="placeholder ?? 'ID или название оффера'"
      :disabled="disabled"
      autocomplete="off"
      @input="onInput"
      @focus="search"
      @keydown="onKey"
    />
    <span v-if="loading" class="search-hint">поиск…</span>
    <ul v-if="open" class="search-list" @mousedown.prevent>
      <li v-if="!results.length" class="muted">Ничего не найдено</li>
      <li
        v-for="(o, i) in results"
        :key="o.id"
        :class="{ hl: i === highlighted }"
        @click="choose(o)"
      ><b>{{ o.id }}</b> {{ offerLabel(o.id, o.name) }}</li>
    </ul>
    <div v-if="error" class="error small">{{ error }}</div>
  </div>
</template>
