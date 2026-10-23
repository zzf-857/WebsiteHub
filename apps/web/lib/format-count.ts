// 数量的中文单位格式化（星数、复刻数等共用）。纯函数，Node 测试直接覆盖。
// 不用 “k”：不足 1 万保留千分位，1 万以上用 “万”，1 亿以上用 “亿”（保留一位小数且截断不进位）。

/** 保留一位小数并截断不进位；整数时不带 “.0”。 */
function truncatedTenths(value: number, unit: number): string {
  const tenths = Math.floor(value / (unit / 10)) / 10;
  return Number.isInteger(tenths) ? tenths.toFixed(0) : tenths.toFixed(1);
}

/** 999 → “999”，6100 → “6,100”，52300 → “5.2 万”，1,200,000 → “120 万”，1.2 亿 → “1.2 亿”。 */
export function formatCount(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "0";
  const whole = Math.floor(value);
  if (whole < 10_000) return String(whole).replace(/\B(?=(\d{3})+(?!\d))/gu, ",");
  if (whole < 100_000_000) return `${truncatedTenths(whole, 10_000)} 万`;
  return `${truncatedTenths(whole, 100_000_000)} 亿`;
}
