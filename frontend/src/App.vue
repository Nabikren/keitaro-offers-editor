<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, ApiError, type AppConfig } from './api'
import CampaignEditor from './components/CampaignEditor.vue'
import CreateCampaign from './components/CreateCampaign.vue'

const tab = ref<'create' | 'editor'>('editor')
const config = ref<AppConfig | null>(null)
const error = ref('')
const openId = ref<number | null>(null)

onMounted(async () => {
  try { config.value = await api.config() }
  catch (e) { error.value = e instanceof ApiError ? e.message : 'Backend недоступен' }
})

function openInEditor(id: number) {
  openId.value = id
  tab.value = 'editor'
}
</script>

<template>
  <header class="top">
    <strong>Keitaro offers editor</strong>
    <nav>
      <button type="button" :class="{ active: tab === 'create' }" @click="tab = 'create'">Новая кампания</button>
      <button type="button" :class="{ active: tab === 'editor' }" @click="tab = 'editor'">Редактор</button>
    </nav>
  </header>
  <main>
    <div v-if="error" class="alert error">{{ error }}</div>
    <div v-else-if="config && !config.keitaro_configured" class="alert warn">
      Keitaro не настроен: задайте KEITARO_URL и KEITARO_API_KEY в .env backend.
    </div>
    <template v-if="config">
      <CreateCampaign v-if="tab === 'create'" :config="config" @open="openInEditor" />
      <CampaignEditor v-show="tab === 'editor'" :initial-id="openId" />
    </template>
  </main>
</template>
