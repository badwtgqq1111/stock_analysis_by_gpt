/** 两页共用后端年化名次；未排名置后且按 id 稳定排列，绝不回退累计收益。 */
export function annualRankOrder(a: number | undefined, b: number | undefined, aId: string, bId: string): number {
  const valid = (n: number | undefined) => n !== undefined && Number.isFinite(n) && n > 0
  const aValid = valid(a), bValid = valid(b)
  if (aValid && bValid && a !== b) return a! - b!
  if (aValid !== bValid) return aValid ? -1 : 1
  return aId.localeCompare(bId, 'en')
}
