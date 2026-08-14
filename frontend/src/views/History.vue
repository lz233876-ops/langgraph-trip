<template>
  <div class="history-container">
    <!-- 页面头部 -->
    <div class="page-header">
      <a-button class="back-button" size="large" @click="goBack">
        ← 返回首页
      </a-button>
      <div class="header-title">
        <span class="header-icon">📜</span>
        <h1>历史行程记录</h1>
      </div>
      <div class="header-search">
        <a-input
          v-model:value="cityFilter"
          placeholder="按城市筛选"
          allow-clear
          style="width: 180px"
          @change="handleSearch"
        >
          <template #prefix>🏙️</template>
        </a-input>
      </div>
    </div>

    <!-- 加载中 -->
    <div v-if="loading" class="loading-wrapper">
      <a-spin size="large" tip="加载历史记录中..." />
    </div>

    <!-- 列表 -->
    <div v-else-if="records.length > 0" class="record-list">
      <a-card
        v-for="record in records"
        :key="record.id"
        class="record-card"
        :bordered="false"
      >
        <div class="record-main">
          <div class="record-city">
            <span class="city-name">{{ record.city }}</span>
            <a-tag color="blue">{{ record.travel_days }} 天</a-tag>
          </div>
          <div class="record-meta">
            <span class="meta-item">📅 {{ record.start_date }} ~ {{ record.end_date }}</span>
            <span class="meta-item">🎯 {{ record.attraction_count }} 个景点</span>
            <span class="meta-item" v-if="record.budget_total">💰 ¥{{ record.budget_total.toLocaleString() }}</span>
            <span class="meta-item">🕐 {{ record.created_at }}</span>
          </div>
          <div class="record-prefs" v-if="record.preferences && record.preferences.length">
            <a-tag
              v-for="p in record.preferences"
              :key="p"
              class="pref-tag"
            >{{ p }}</a-tag>
          </div>
        </div>
        <div class="record-actions">
          <a-button type="primary" @click="viewRecord(record.id)">
            👁️ 查看行程
          </a-button>
          <a-popconfirm title="确定删除这条历史记录吗?" @confirm="removeRecord(record.id)">
            <a-button danger>🗑️ 删除</a-button>
          </a-popconfirm>
        </div>
      </a-card>

      <!-- 分页 -->
      <div class="pagination-wrapper" v-if="total > pageSize">
        <a-pagination
          v-model:current="page"
          :total="total"
          :page-size="pageSize"
          :show-total="(t: number) => `共 ${t} 条记录`"
          @change="loadRecords"
        />
      </div>
    </div>

    <!-- 空状态 -->
    <a-empty v-else class="empty-wrapper">
      <template #image>
        <div style="font-size: 64px;">🗺️</div>
      </template>
      <template #description>
        <span>还没有历史行程记录，快去生成你的第一个旅行计划吧</span>
      </template>
      <a-button type="primary" @click="goBack">返回首页创建行程</a-button>
    </a-empty>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { fetchHistory, fetchHistoryDetail, deleteHistory } from '@/services/api'

const router = useRouter()

const records = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 10
const cityFilter = ref('')
const loading = ref(false)

onMounted(() => {
  loadRecords()
})

const goBack = () => {
  router.push('/')
}

// 城市筛选 (防抖由 input change 触发)
let searchTimer: ReturnType<typeof setTimeout> | null = null
const handleSearch = () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    page.value = 1
    loadRecords()
  }, 400)
}

// 加载列表
const loadRecords = async () => {
  loading.value = true
  try {
    const resp = await fetchHistory(page.value, pageSize, cityFilter.value.trim())
    if (resp.success) {
      records.value = resp.data
      total.value = resp.total
    }
  } catch (error: any) {
    message.error(error.message || '加载历史记录失败')
  } finally {
    loading.value = false
  }
}

// 查看行程: 拉详情后跳转结果页渲染 (记录 id, 供结果页编辑保存写回数据库)
const viewRecord = async (id: number) => {
  try {
    const resp = await fetchHistoryDetail(id)
    if (resp.success && resp.data.plan) {
      sessionStorage.setItem('tripPlan', JSON.stringify(resp.data.plan))
      sessionStorage.setItem('tripPlanId', String(id))
      router.push('/result')
    } else {
      message.error('记录数据异常')
    }
  } catch (error: any) {
    message.error(error.message || '加载行程详情失败')
  }
}

// 删除记录
const removeRecord = async (id: number) => {
  try {
    const resp = await deleteHistory(id)
    if (resp.success) {
      message.success('删除成功')
      // 当前页删空后回退一页
      if (records.value.length === 1 && page.value > 1) {
        page.value -= 1
      }
      loadRecords()
    }
  } catch (error: any) {
    message.error(error.message || '删除失败')
  }
}
</script>

<style scoped>
.history-container {
  min-height: 100vh;
  background: transparent;
  padding: 40px 20px;
}

.page-header {
  max-width: 900px;
  margin: 0 auto 30px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  animation: fadeInDown 0.6s ease-out;
}

.back-button {
  border-radius: 8px;
  font-weight: 500;
}

.header-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.header-title h1 {
  margin: 0;
  font-size: 28px;
  color: #fff;
  text-shadow: 0 2px 12px rgba(139, 92, 246, 0.4);
}

.header-icon {
  font-size: 28px;
}

.record-list {
  max-width: 900px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.record-card {
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.93) !important;
  backdrop-filter: blur(16px) saturate(140%);
  border: 1px solid rgba(255, 255, 255, 0.5) !important;
  box-shadow: 0 12px 36px rgba(2, 6, 23, 0.35);
  transition: all 0.3s ease;
  animation: fadeInUp 0.5s ease-out;
}

.record-card:hover {
  transform: translateY(-4px);
  box-shadow: 0 20px 48px rgba(2, 6, 23, 0.5);
}

.record-main {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.record-city {
  display: flex;
  align-items: center;
  gap: 10px;
}

.city-name {
  font-size: 22px;
  font-weight: 700;
  color: #333;
}

.record-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}

.meta-item {
  font-size: 14px;
  color: #666;
}

.record-prefs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.pref-tag {
  border-radius: 12px;
}

.record-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 6px;
}

.pagination-wrapper {
  display: flex;
  justify-content: center;
  padding: 20px 0;
}

.loading-wrapper {
  display: flex;
  justify-content: center;
  padding: 80px 0;
}

.empty-wrapper {
  padding: 80px 0;
}

@keyframes fadeInDown {
  from {
    opacity: 0;
    transform: translateY(-20px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes fadeInUp {
  from {
    opacity: 0;
    transform: translateY(20px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
