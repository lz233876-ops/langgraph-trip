import axios from 'axios'
import type { TripFormData, TripPlanResponse } from '@/types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 300000, // 2分钟超时
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
apiClient.interceptors.request.use(
  (config) => {
    console.log('发送请求:', config.method?.toUpperCase(), config.url)
    return config
  },
  (error) => {
    console.error('请求错误:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    console.log('收到响应:', response.status, response.config.url)
    return response
  },
  (error) => {
    console.error('响应错误:', error.response?.status, error.message)
    return Promise.reject(error)
  }
)

/**
 * 生成旅行计划
 */
export async function generateTripPlan(formData: TripFormData): Promise<TripPlanResponse> {
  try {
    const response = await apiClient.post<TripPlanResponse>('/api/trip/plan', formData)
    return response.data
  } catch (error: any) {
    console.error('生成旅行计划失败:', error)
    throw new Error(error.response?.data?.detail || error.message || '生成旅行计划失败')
  }
}

/**
 * 健康检查
 */
export async function healthCheck(): Promise<any> {
  try {
    const response = await apiClient.get('/health')
    return response.data
  } catch (error: any) {
    console.error('健康检查失败:', error)
    throw new Error(error.message || '健康检查失败')
  }
}

/**
 * 查询历史行程列表 (分页)
 */
export async function fetchHistory(
  page: number = 1,
  pageSize: number = 10,
  city?: string
): Promise<any> {
  const response = await apiClient.get('/api/history', {
    params: { page, page_size: pageSize, city: city || undefined }
  })
  return response.data
}

/**
 * 查询历史行程详情 (含完整计划)
 */
export async function fetchHistoryDetail(id: number): Promise<any> {
  const response = await apiClient.get(`/api/history/${id}`)
  return response.data
}

/**
 * 更新历史行程 (编辑保存后持久化)
 */
export async function updateHistory(id: number, plan: any): Promise<any> {
  const response = await apiClient.put(`/api/history/${id}`, plan)
  return response.data
}

/**
 * 删除历史行程
 */
export async function deleteHistory(id: number): Promise<any> {
  const response = await apiClient.delete(`/api/history/${id}`)
  return response.data
}

export default apiClient

