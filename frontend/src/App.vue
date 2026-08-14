<template>
  <div id="app">
    <router-view v-slot="{ Component }">
      <transition name="page" mode="out-in">
        <component :is="Component" />
      </transition>
    </router-view>
  </div>
</template>

<script setup lang="ts">
</script>

<style>
#app {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC',
    'Microsoft YaHei', 'Noto Sans SC', sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  position: relative;
  z-index: 1;
  min-height: 100vh;
}

body {
  margin: 0;
  min-height: 100vh;
  /* 深空霓虹渐变背景 */
  background:
    radial-gradient(1100px 600px at 10% -10%, rgba(124, 58, 237, 0.38), transparent 55%),
    radial-gradient(900px 560px at 90% 5%, rgba(56, 130, 246, 0.32), transparent 55%),
    radial-gradient(1000px 700px at 55% 115%, rgba(16, 185, 129, 0.25), transparent 55%),
    linear-gradient(135deg, #070a1f 0%, #120f3f 45%, #2a1458 100%);
  background-attachment: fixed;
}

/* 网格纹理层 */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background-image:
    linear-gradient(rgba(255, 255, 255, 0.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, 0.035) 1px, transparent 1px);
  background-size: 46px 46px;
}

/* 霓虹光斑浮动层 */
body::after {
  content: '';
  position: fixed;
  inset: -20%;
  z-index: 0;
  pointer-events: none;
  background:
    radial-gradient(420px 320px at 22% 26%, rgba(139, 92, 246, 0.5), transparent 65%),
    radial-gradient(380px 300px at 78% 34%, rgba(59, 130, 246, 0.42), transparent 65%),
    radial-gradient(460px 360px at 60% 88%, rgba(16, 185, 129, 0.34), transparent 65%);
  animation: nebulaFloat 16s ease-in-out infinite alternate;
}

@keyframes nebulaFloat {
  0% {
    transform: translate3d(0, 0, 0) scale(1);
  }
  100% {
    transform: translate3d(2%, -3%, 0) scale(1.06);
  }
}

/* 玻璃拟态卡片 (各页面复用) */
.glass-card {
  background: rgba(255, 255, 255, 0.92) !important;
  backdrop-filter: blur(20px) saturate(150%);
  border: 1px solid rgba(255, 255, 255, 0.55) !important;
  box-shadow: 0 20px 50px rgba(2, 6, 23, 0.45);
}

/* 全局主按钮渐变主题 */
.ant-btn-primary {
  background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
  border: none;
  box-shadow: 0 4px 14px rgba(99, 102, 241, 0.35);
  transition: all 0.3s ease;
}

.ant-btn-primary:hover,
.ant-btn-primary:focus {
  background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%) !important;
  box-shadow: 0 6px 22px rgba(99, 102, 241, 0.5);
  transform: translateY(-1px);
}

/* 页面切换过渡动画 */
.page-enter-active,
.page-leave-active {
  transition: opacity 0.3s ease, transform 0.3s ease;
}
.page-enter-from {
  opacity: 0;
  transform: translateY(12px);
}
.page-leave-to {
  opacity: 0;
  transform: translateY(-12px);
}

/* 全局滚动条美化 */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}
::-webkit-scrollbar-track {
  background: transparent;
}
::-webkit-scrollbar-thumb {
  background: rgba(139, 92, 246, 0.45);
  border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover {
  background: rgba(139, 92, 246, 0.75);
}
</style>
