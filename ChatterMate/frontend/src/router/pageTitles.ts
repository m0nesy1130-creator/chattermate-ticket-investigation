/*
Copyright 2024-2026 ChatterMate

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

import type { RouteLocationNormalizedLoaded } from 'vue-router'
import { formatBadgeCount } from '@/utils/badgeCount'

export const APP_NAME = 'ChatterMate'

/**
 * The name of every dashboard page, keyed by its route path pattern.
 *
 * One source for the sidebar label, the header breadcrumb and the browser tab
 * title, so the three cannot disagree. A path missing here (login, setup, the
 * Shopify views) gets the plain app name in the tab and no breadcrumb.
 */
export const PAGE_TITLES = {
  '/ai-agents': 'AI 助手',
  '/human-agents': '人工客服',
  '/conversations': '收件箱',
  '/tickets': '工单',
  '/tickets/:id': '工单详情',
  '/workbench': '分析工作台',
  '/workbench/:id': '工单分析',
  '/people': '联系人',
  '/knowledge': '知识库',
  '/faq': '帮助中心',
  '/analytics': '数据分析',
  '/settings/organization': '组织',
  '/settings/subscription': '订阅',
  '/settings/subscription/setup/:planId': '订阅',
  '/settings/ticketing': '工单设置',
  '/settings/saved-replies': '快捷回复',
  '/settings/integrations': '集成',
  '/settings/app-integrations': '应用集成',
  '/settings/widget-apps': '组件应用',
  '/settings/ai-config': 'AI 配置',
  '/settings/user': '用户设置',
  '/settings/access-tokens': 'CLI 与 API 令牌',
} as const satisfies Readonly<Record<string, string>>

/** A route path pattern that has a page title. */
export type PagePath = keyof typeof PAGE_TITLES

const TITLE_BY_PATH: Readonly<Record<string, string>> = PAGE_TITLES

/** A titled page's name. Typed, so a nav link to an untitled page fails the build. */
export const pageTitle = (path: PagePath): string => PAGE_TITLES[path]

/** The current page's name, from the deepest matched route record. */
export function routePageTitle(route: Pick<RouteLocationNormalizedLoaded, 'matched'>): string | undefined {
  const record = route.matched[route.matched.length - 1]
  return record ? TITLE_BY_PATH[record.path] : undefined
}

/**
 * The browser tab title: "Analytics · ChatterMate", "(3) Inbox · ChatterMate"
 * when there is something waiting, or just the app name off the dashboard.
 */
export function documentTitle(page?: string, count?: number | null): string {
  if (!page) return APP_NAME
  const prefix = count ? `(${formatBadgeCount(count)}) ` : ''
  return `${prefix}${page} · ${APP_NAME}`
}
