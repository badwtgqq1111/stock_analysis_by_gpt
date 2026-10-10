/**
 * Squarified treemap 布局（Bruls/Huizing/van Wijk 2000 的经典算法）。
 *
 * 为什么自己写：项目里没有 d3，而"面积=流通市值"的热力图必须用矩形树图
 * （裁成方格的写法在 111 个行业里会退化成细条，行业名根本放不下）。
 * 这个函数是纯函数、无 DOM 依赖，`test` 之外也可被别的页面复用。
 */

export interface Rect {
  x: number
  y: number
  w: number
  h: number
}

export interface TreemapItem<T> {
  value: number
  data: T
}

export interface TreemapCell<T> {
  rect: Rect
  data: T
  /** 原数组下标（调用方可据此把颜色/交互映射回去） */
  index: number
}

/** 一行里"最差长宽比"：越小越接近正方形。 */
function worstRatio(areas: number[], side: number): number {
  if (side <= 0) return Number.POSITIVE_INFINITY
  const sum = areas.reduce((a, b) => a + b, 0)
  if (sum <= 0) return Number.POSITIVE_INFINITY
  const thickness = sum / side
  let worst = 0
  for (const area of areas) {
    if (area <= 0) continue
    const length = area / thickness
    const ratio = Math.max(length / thickness, thickness / length)
    if (ratio > worst) worst = ratio
  }
  return worst
}

/**
 * 把 `items` 按 `value` 比例铺满 `bounds`。
 * 返回顺序与**输入顺序一一对应**（内部按 value 降序排列后再还原），
 * 这样调用方可以放心用 `index` 去索引自己的数组。
 */
export function squarify<T>(items: TreemapItem<T>[], bounds: Rect): TreemapCell<T>[] {
  const cells: TreemapCell<T>[] = new Array(items.length)
  if (!items.length || bounds.w <= 0 || bounds.h <= 0) {
    items.forEach((item, index) => {
      cells[index] = { rect: { x: bounds.x, y: bounds.y, w: 0, h: 0 }, data: item.data, index }
    })
    return cells
  }

  const order = items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => (b.item.value || 0) - (a.item.value || 0))

  const total = order.reduce((sum, entry) => sum + Math.max(0, entry.item.value || 0), 0)
  // 全零（或负值）时退化成等分：布局仍稳定，不会出现 NaN 宽度把 canvas 画崩
  const equalArea = (bounds.w * bounds.h) / order.length
  const scale = total > 0 ? (bounds.w * bounds.h) / total : 0

  let x = bounds.x
  let y = bounds.y
  let w = bounds.w
  let h = bounds.h
  let pos = 0

  const areaOf = (entry: { item: TreemapItem<T> }) =>
    total > 0 ? Math.max(0, entry.item.value || 0) * scale : equalArea

  while (pos < order.length) {
    const side = Math.min(w, h)
    if (side <= 1e-6) {
      for (let k = pos; k < order.length; k += 1) {
        cells[order[k].index] = { rect: { x, y, w: 0, h: 0 }, data: order[k].item.data, index: order[k].index }
      }
      break
    }

    let row: { item: TreemapItem<T>; index: number }[] = []
    let rowAreas: number[] = []
    let rowSum = 0
    while (pos < order.length) {
      const nextArea = areaOf(order[pos])
      const candidateAreas = [...rowAreas, nextArea]
      const candidateWorst = worstRatio(candidateAreas, side)
      if (row.length && candidateWorst > worstRatio(rowAreas, side)) break
      row = [...row, order[pos]]
      rowAreas = candidateAreas
      rowSum += nextArea
      pos += 1
    }

    // 行沿"短边"铺：w >= h 时短边是 h，行就是从左边切走的一条竖条
    if (w >= h) {
      const stripW = rowSum / h
      let cursor = y
      for (const entry of row) {
        const area = areaOf(entry)
        const cellH = stripW > 0 ? area / stripW : 0
        cells[entry.index] = {
          rect: { x, y: cursor, w: stripW, h: cellH },
          data: entry.item.data,
          index: entry.index,
        }
        cursor += cellH
      }
      x += stripW
      w -= stripW
    } else {
      const stripH = rowSum / w
      let cursor = x
      for (const entry of row) {
        const area = areaOf(entry)
        const cellW = stripH > 0 ? area / stripH : 0
        cells[entry.index] = {
          rect: { x: cursor, y, w: cellW, h: stripH },
          data: entry.item.data,
          index: entry.index,
        }
        cursor += cellW
      }
      y += stripH
      h -= stripH
    }
  }

  return cells
}

/**
 * 命中测试：从后往前找（后画的在上层）。
 * 用 `{ rect }` 约束而不是 `TreemapCell<T>`，这样调用方可以把自己的 cell 类型
 * （带 label/颜色等额外字段）直接传进来，不必再包一层。
 */
export function hitTest<T extends { rect: Rect }>(cells: T[], px: number, py: number): T | null {
  for (let i = cells.length - 1; i >= 0; i -= 1) {
    const { rect } = cells[i]
    if (rect.w <= 0 || rect.h <= 0) continue
    if (px >= rect.x && px <= rect.x + rect.w && py >= rect.y && py <= rect.y + rect.h) return cells[i]
  }
  return null
}
