<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const facts = ref([])
const boundsInfo = ref(null)
const err = ref('')

async function load() {
  err.value = ''
  try {
    facts.value = await api.dwellFacts()
    boundsInfo.value = await api.dwellBounds()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

const allVerified = computed(
  () => facts.value.length > 0 && facts.value.every((f) => f.verified),
)

function fmtBound(b) {
  return b.max_sec === null ? `${b.name}（开放上界）` : `${b.name} < ${b.max_sec}s`
}

onMounted(load)
</script>

<template>
  <section class="panel">
    <h1>停留分桶核证</h1>
    <p class="muted">
      只读视图：会话、停留秒、桶界版本、桶名逐条复算比对；桶名须由起止（缺失则末击点）派生。
    </p>
    <p v-if="err" class="err">{{ err }}</p>

    <div class="stats" v-if="boundsInfo">
      <div class="stat"><b>v{{ boundsInfo.current_version }}</b><span>现行桶界</span></div>
      <div class="stat">
        <b>{{ facts.length }}</b><span>分桶事实</span>
      </div>
      <div class="stat">
        <b :class="allVerified ? 'ok' : 'err'">{{ allVerified ? '一致' : '有分叉' }}</b>
        <span>复算结论</span>
      </div>
    </div>

    <div class="bounds-strip" v-if="boundsInfo">
      <span
        v-for="b in boundsInfo.versions.find((v) => v.version === boundsInfo.current_version)?.bounds || []"
        :key="b.seq"
        class="bucket-chip"
      >
        {{ fmtBound(b) }}
      </span>
    </div>

    <table class="data">
      <thead>
        <tr>
          <th>会话</th>
          <th>起止（会话）</th>
          <th>结束依据</th>
          <th>停留秒</th>
          <th>桶界版本</th>
          <th>桶名</th>
          <th>复算</th>
          <th>修订</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="f in facts" :key="f.session_id">
          <td class="mono">{{ f.session_id }}</td>
          <td class="mono">
            {{ f.session_started_at }} → {{ f.session_ended_at || '…' }}
          </td>
          <td class="mono">{{ f.ended_snap ? '起止' : '末击点 ' + f.effective_end }}</td>
          <td class="mono">{{ f.dwell_sec }}s</td>
          <td class="mono">v{{ f.bounds_version }}</td>
          <td>
            <span class="bucket-chip">{{ f.bucket_name }}</span>
          </td>
          <td>
            <span :class="f.verified ? 'ok' : 'err'" class="mono">
              {{ f.verified ? '一致' : `应为 ${f.expected_bucket_name} / ${f.expected_dwell_sec}s` }}
            </span>
          </td>
          <td class="mono">r{{ f.revision }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="!facts.length && !err" class="muted">暂无分桶事实。</p>
  </section>
</template>
