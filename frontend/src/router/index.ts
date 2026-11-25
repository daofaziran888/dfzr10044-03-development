import { createRouter, createWebHistory } from 'vue-router'

import EvolveDetailView from '@/views/EvolveDetailView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径必须放在动态 :moduleKey 之前，避免被工作台路由抢先匹配。
    { path: '/partition_evolve/handoff/:id', name: 'evolve-detail', component: EvolveDetailView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
