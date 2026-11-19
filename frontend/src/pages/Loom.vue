<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const overview = ref(null)
const sessions = ref([])
const details = ref({})
const err = ref('')
const notice = ref({})
const edits = ref({})
const busy = ref('')

function toLocal(ts) {
  // 存储统一为 YYYY-MM-DDTHH:MM:SSZ；编辑框沿用同一格式，避免本地时区歧义。
  return ts || ''
}

function syncEdits(rows) {
  const next = {}
  for (const s of rows) {
    const prev = edits.value[s.id]
    next[s.id] = {
      started_at: prev?.started_at ?? toLocal(s.started_at),
      ended_at: prev?.ended_at ?? toLocal(s.ended_at),
      manual_bucket: prev?.manual_bucket ?? '',
    }
  }
  edits.value = next
}

async function load() {
  err.value = ''
  try {
    overview.value = await api.overview()
    sessions.value = await api.sessions()
    const map = {}
    for (const s of sessions.value) {
      map[s.id] = await api.session(s.id)
    }
    details.value = map
    syncEdits(sessions.value)
  } catch (e) {
    err.value = String(e.message || e)
  }
}

function say(id, text, ok = true) {
  notice.value = { ...notice.value, [id]: { text, ok } }
}

async function saveSpan(s) {
  const e = edits.value[s.id]
  busy.value = s.id
  try {
    const payload = {}
    if (e.started_at !== (s.started_at || '')) payload.started_at = e.started_at
    if (e.ended_at !== (s.ended_at || '')) payload.ended_at = e.ended_at === '' ? null : e.ended_at
    if (!Object.keys(payload).length) {
      say(s.id, '起止未改动', false)
      return
    }
    const res = await api.updateSpan(s.id, payload)
    say(s.id, `起止与桶名同事务落库：${res.dwell.dwell_sec}s → ${res.dwell.bucket_name}`, true)
    await load()
  } catch (ev) {
    say(s.id, String(ev.message || ev), false)
    await load()
  } finally {
    busy.value = ''
  }
}

async function closeSession(s) {
  busy.value = s.id
  try {
    const res = await api.closeSession(s.id, {})
    say(
      s.id,
      `会话已关闭；桶名随起止重算为 ${res.dwell.bucket_name}（${res.dwell.dwell_sec}s）`,
      true,
    )
    await load()
  } catch (ev) {
    say(s.id, String(ev.message || ev), false)
    await load()
  } finally {
    busy.value = ''
  }
}

async function tryManualBucket(s) {
  const e = edits.value[s.id]
  if (!e.manual_bucket) {
    say(s.id, '先填一个桶名再试', false)
    return
  }
  busy.value = s.id
  try {
    await api.patchSession(s.id, { dwell_bucket: e.manual_bucket })
    say(s.id, '异常：手改桶名竟然成功了', false)
  } catch (ev) {
    say(s.id, `手改桶名整笔失败：${ev.message}`, true)
  } finally {
    busy.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="stats" v-if="overview">
      <div class="stat"><b>{{ overview.visitors }}</b><span>访客</span></div>
      <div class="stat"><b>{{ overview.sessions }}</b><span>会话</span></div>
      <div class="stat"><b>{{ overview.hits_primary }}</b><span>正式击点</span></div>
      <div class="stat"><b>{{ overview.quarantine }}</b><span>隔离</span></div>
      <div class="stat"><b>{{ overview.idle_timeout_sec }}s</b><span>空闲超时</span></div>
    </div>
    <p v-if="err" class="err">{{ err }}</p>

    <section class="panel">
      <h1>会话织机</h1>
      <p class="muted">
        停留秒由会话起止算出（缺失时退到末击点），按桶界版本物化为桶名；改起止即同事务改桶名。
      </p>
      <div class="lane-list">
        <article v-for="s in sessions" :key="s.id" class="lane">
          <div class="lane-head">
            <div>
              <strong>{{ s.id }}</strong>
              <span class="muted"> · {{ s.visitor_id }} · {{ s.status }}</span>
            </div>
            <span class="mono">{{ s.hit_count }} 点 · {{ s.started_at }} → {{ s.ended_at || '…' }}</span>
          </div>

          <div class="bucket-row">
            <span class="bucket-chip">{{ s.dwell_bucket || '未物化' }}</span>
            <span class="mono muted">
              停留 {{ s.dwell_sec ?? '—' }}s
              <template v-if="s.dwell"> · 桶界 v{{ s.dwell.bounds_version }} · r{{ s.dwell.revision }}</template>
            </span>
          </div>

          <div class="beads" v-if="details[s.id]">
            <template v-for="(h, i) in details[s.id].hits" :key="h.id">
              <span v-if="i" class="connector" aria-hidden="true"></span>
              <span class="hit-bead">
                <strong>{{ h.event_name }}</strong>
                <span>{{ h.page_path }}</span>
              </span>
            </template>
          </div>

          <div class="span-form" v-if="edits[s.id]">
            <label>
              起
              <input v-model="edits[s.id].started_at" class="mono" placeholder="2026-11-17T08:10:00Z" />
            </label>
            <label>
              止
              <input v-model="edits[s.id].ended_at" class="mono" placeholder="留空表示用末击点" />
            </label>
            <button class="btn" :disabled="busy === s.id" @click="saveSpan(s)">存起止并重物化</button>
            <button class="btn ghost" :disabled="busy === s.id || s.status === 'closed'" @click="closeSession(s)">
              关闭会话
            </button>
          </div>

          <div class="span-form" v-if="edits[s.id]">
            <label>
              手改桶名
              <input v-model="edits[s.id].manual_bucket" class="mono" placeholder="试填 300s+" />
            </label>
            <button class="btn danger" :disabled="busy === s.id" @click="tryManualBucket(s)">
              试手改（应失败）
            </button>
          </div>

          <p v-if="notice[s.id]" :class="notice[s.id].ok ? 'ok' : 'err'" class="mono notice">
            {{ notice[s.id].text }}
          </p>
        </article>
      </div>
    </section>
  </div>
</template>
