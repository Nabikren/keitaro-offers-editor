<script setup lang="ts">
defineProps<{ title: string; text: string; confirmLabel: string; danger?: boolean }>()
defineEmits<{ confirm: []; cancel: [] }>()
</script>

<template>
  <div class="overlay" role="dialog" aria-modal="true" @keydown.esc="$emit('cancel')">
    <div class="dialog">
      <h3>{{ title }}</h3>
      <p>{{ text }}</p>
      <div class="row end">
        <button type="button" @click="$emit('cancel')">Отмена</button>
        <button type="button" :class="danger ? 'danger' : 'primary'" @click="$emit('confirm')">{{ confirmLabel }}</button>
      </div>
    </div>
  </div>
</template>
