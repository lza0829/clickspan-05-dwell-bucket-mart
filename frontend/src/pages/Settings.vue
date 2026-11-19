<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const sec = ref(1800)
const msg = ref('')
const err = ref('')

const bounds = ref([])
const currentVersion = ref(null)
const boundsMsg = ref('')
const boundsErr = ref('')

const draft = computed(() =>
  bounds.value.map((b) => ({
    name: b.name,
    max_sec: b.max_sec === null ? '' : String(b.max_sec),
  })),
)

function payload() {
  return draft.value.map((b, i) => ({
    name: b.name,
    max_sec:
      i === draft.value.length - 1
        ? null
        : b.max_sec === ''
          ? null
          : Number(b.max_sec),
  }))
}

async function loadBounds() {
  boundsErr.value = ''
  try {
    const data = await api.dwellBounds()
    currentVersion.value = data.current_version
    const cur = data.versions.find((v) => v.version === data.current_version)
    bounds.value = (cur?.bounds || []).map((b) => ({ name: b.name, max_sec: b.max_sec }))
  } catch (e) {
    boundsErr.value = String(e.message || e)
  }
}

onMounted(async () => {
  try {
    const s = await api.settings()
    sec.value = s.idle_timeout_sec
  } catch (e) {
    err.value = String(e.message || e)
  }
  await loadBounds()
})

async function save() {
  err.value = ''
  msg.value = ''
  try {
    const res = await fetch('/api/settings/idle-timeout', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ idle_timeout_sec: Number(sec.value) }),
    })
    if (!res.ok) throw new Error(await res.text())
    const data = await res.json()
    sec.value = data.idle_timeout_sec
    msg.value = '已保存（基座装载尚未按此时切开）'
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function saveBounds() {
  boundsMsg.value = ''
  boundsErr.value = ''
  try {
    const res = await api.setDwellBounds(payload())
    boundsMsg.value = `桶界升到 v${res.version}，已整批重物化 ${res.rematerialized} 个会话`
    await loadBounds()
  } catch (e) {
    boundsErr.value = String(e.message || e)
  }
}
</script>

<template>
  <section class="panel">
    <h1>空闲切窗</h1>
    <p class="muted">配置写入 settings；真正按间隙切会话由功能题空桩实现。</p>
    <label>
      秒数
      <input v-model.number="sec" type="number" min="60" step="60" class="mono" />
    </label>
    <p>
      <button class="btn" @click="save">保存</button>
    </p>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="err">{{ err }}</p>
  </section>

  <section class="panel" style="margin-top: 1rem">
    <h2>停留秒桶界</h2>
    <p class="muted">
      桶按停留秒的开区间划分（停留秒 &lt; 上界）；最后一档留空表示开放上界。
      保存即生成新桶界版本，并把全部会话重物化到新版本。
    </p>
    <p class="mono muted" v-if="currentVersion">现行版本 v{{ currentVersion }}</p>
    <div class="bounds-editor">
      <div v-for="(b, i) in draft" :key="i" class="bounds-line">
        <label>
          桶名
          <input v-model="b.name" class="mono" />
        </label>
        <label>
          上界（秒）
          <input
            v-model="b.max_sec"
            class="mono"
            :placeholder="i === draft.length - 1 ? '开放上界' : '如 60'"
            :disabled="i === draft.length - 1"
          />
        </label>
      </div>
    </div>
    <p>
      <button class="btn" @click="saveBounds">保存桶界并重物化</button>
    </p>
    <p v-if="boundsMsg" class="ok">{{ boundsMsg }}</p>
    <p v-if="boundsErr" class="err">{{ boundsErr }}</p>
  </section>
</template>
