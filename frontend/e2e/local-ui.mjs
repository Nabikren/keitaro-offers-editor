// Браузерный прогон UI против настоящего локального backend + SQLite и фейкового Keitaro
// (python -m app.devtools.fake_keitaro). Live-стенд не используется.
// Запуск: npm run e2e  (нужны запущенные fake Keitaro :8090, backend :8000, vite :5173 и установленный Chrome)
import { chromium } from 'playwright-core'
import assert from 'node:assert/strict'

const APP = process.env.APP_URL ?? 'http://127.0.0.1:5173'
const FAKE = process.env.FAKE_URL ?? 'http://127.0.0.1:8090'
const SHOTS = process.env.SHOTS_DIR ?? ''
const CAMPAIGN = 5000, STREAM = 5002

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, channel: process.env.CHROME_PATH ? undefined : 'chrome' })
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
const consoleErrors = []
const httpLog = []
// Браузер пишет «Failed to load resource» для любых 4xx: ожидаемые конфликты (409) проверяются шагами,
// поэтому здесь фиксируем все ответы отдельно, а ошибкой считаем JS-ошибки и любые 5xx.
page.on('console', (m) => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) consoleErrors.push(m.text()) })
page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + e.message))
page.on('response', (r) => { if (r.status() >= 400) httpLog.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`) })
page.on('dialog', (d) => { consoleErrors.push('native dialog: ' + d.message()); d.dismiss() })

const step = async (name, fn) => { await fn(); console.log('✓', name) }
const shot = async (name) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true }) }
const card = () => page.locator(`[data-stream="${STREAM}"]`)
const row = (offer) => card().locator(`tr[data-offer="${offer}"]`)
const shares = async () => (await card().locator('tbody .share-value').allTextContents()).map((t) => parseInt(t))
const remote = async () => (await (await fetch(`${FAKE}/__fake/streams/${STREAM}`)).json()).offers.map((o) => [o.offer_id, o.share])
const confirmDialog = async (label) => { await page.getByRole('dialog').getByRole('button', { name: label }).click(); await page.getByRole('dialog').waitFor({ state: 'detached' }) }
const waitIdle = () => card().locator('button:has-text("Remove")').first().waitFor({ state: 'visible' })
const pushAndConfirm = async () => {
  await card().getByRole('button', { name: 'Push to KT' }).click()
  await confirmDialog('Push')
  await card().locator('.alert.ok').waitFor()
}
const addOffer = async (query, id) => {
  const input = card().locator('.add input')
  await input.fill(query)
  await card().locator(`.search-list li:has-text("${id}")`).first().click()
  await card().getByRole('button', { name: 'Добавить' }).click()
  await row(id).waitFor()
}

await step('Fetch streams: поток без офферов и поток с 33/33/34', async () => {
  await page.goto(APP)
  await page.locator('#e-id').fill(String(CAMPAIGN))
  await page.getByRole('button', { name: 'Fetch streams from KT' }).click()
  await card().waitFor()
  assert.match(await page.locator('[data-stream="5001"]').innerText(), /Поток без офферов/)
  assert.deepEqual(await shares(), [33, 33, 34])
  await shot('01-fetch')
})

await step('Add через поиск backend → 4×25, поток dirty, Keitaro не изменён', async () => {
  await addOffer('Oxys', 11112)
  assert.deepEqual(await shares(), [25, 25, 25, 25])
  assert.match(await card().innerText(), /не опубликовано/)
  assert.equal((await remote()).length, 3)
  await shot('02-add-dirty')
})

await step('Перезагрузка страницы сохраняет черновик (он в SQLite, не в памяти UI)', async () => {
  await page.reload()
  await page.locator('#e-id').fill(String(CAMPAIGN))
  await page.locator('select[aria-label="Загруженные кампании"]').selectOption(String(CAMPAIGN))
  await card().waitFor()
  assert.deepEqual(await shares(), [25, 25, 25, 25])
  assert.match(await card().innerText(), /не опубликовано/)
})

await step('Push to KT → подтверждено чтением, поток чистый', async () => {
  await pushAndConfirm()
  assert.deepEqual(await remote(), [[3749, 25], [3717, 25], [11111, 25], [11112, 25]])
  assert.doesNotMatch(await card().innerText(), /не опубликовано/)
})

await step('Remove → серый 0% → Push → исчез из Keitaro, остался в архиве', async () => {
  await row(3717).getByRole('button', { name: 'Remove' }).click()
  await row(3717).getByRole('button', { name: 'Bring back' }).waitFor()
  assert.match(await row(3717).innerText(), /0%/)
  assert.match(await row(3717).getAttribute('class'), /removed/)
  await pushAndConfirm()
  assert.ok(!(await remote()).some(([id]) => id === 3717))
  assert.match(await row(3717).innerText(), /архив/)
  await shot('03-archive')
})

await step('Bring back → Push → снова в Keitaro', async () => {
  await row(3717).getByRole('button', { name: 'Bring back' }).click()
  await row(3717).getByRole('button', { name: 'Remove' }).waitFor()
  await pushAndConfirm()
  assert.ok((await remote()).some(([id]) => id === 3717))
})

await step('Cancel удаляет неопубликованную связь', async () => {
  await addOffer('Flexacil', 24800)
  await card().getByRole('button', { name: 'Cancel' }).click()
  await confirmDialog('Отменить изменения')
  await row(24800).waitFor({ state: 'detached' })
  assert.doesNotMatch(await card().innerText(), /не опубликовано/)
})

await step('Удаление в Keitaro + Fetch → связи в архиве, Bring back доступен', async () => {
  await fetch(`${FAKE}/__fake/streams/${STREAM}/remove_offer/3749`, { method: 'POST' })
  await page.getByRole('button', { name: 'Fetch streams from KT' }).click()
  await page.locator('.alert.ok:has-text("удалены в Keitaro")').waitFor()
  assert.match(await row(3749).innerText(), /архив/)
  await row(3749).getByRole('button', { name: 'Bring back' }).waitFor()
})

await step('Pin 25% → Bring back пересчитывает только незакреплённых', async () => {
  await row(11112).getByRole('button', { name: 'pin' }).click()
  await row(11112).locator('input[type=number]').fill('25')
  await row(11112).getByRole('button', { name: 'OK' }).click()
  await row(11112).locator('.pin.on').waitFor()
  await row(3749).getByRole('button', { name: 'Bring back' }).click()
  await row(3749).getByRole('button', { name: 'Remove' }).waitFor()
  assert.match(await row(11112).innerText(), /25%/)
  const s = await shares()
  assert.equal(s.reduce((a, b) => a + b, 0), 100)
  await shot('04-pin')
  await pushAndConfirm()
  assert.ok((await remote()).some(([id, sh]) => id === 11112 && sh === 25))
})

await step('Ошибка правила весов показывается и не меняет данные', async () => {
  await row(3717).getByRole('button', { name: 'pin' }).click()
  await row(3717).locator('input[type=number]').fill('90')
  const before = await shares()
  await row(3717).getByRole('button', { name: 'OK' }).click()
  await card().locator('.alert.error').waitFor()
  assert.match(await card().locator('.alert.error').innerText(), /больше 100%/)
  assert.deepEqual(await shares(), before)
  await row(3717).getByRole('button', { name: '×' }).click()
})

await step('Fetch при черновике спрашивает подтверждение', async () => {
  await addOffer('Flexacil', 24800)
  await page.getByRole('button', { name: 'Fetch streams from KT' }).click()
  await page.getByRole('dialog').waitFor()
  assert.match(await page.getByRole('dialog').innerText(), new RegExp(String(STREAM)))
  await page.getByRole('dialog').getByRole('button', { name: 'Отмена' }).click()
  await row(24800).waitFor() // черновик не тронут
  await page.getByRole('button', { name: 'Fetch streams from KT' }).click()
  await confirmDialog('Заменить данными Keitaro')
  await row(24800).waitFor({ state: 'detached' })
})

await step('Побочный эффект Push: красный СТОП, Push заблокирован до Fetch', async () => {
  await fetch(`${FAKE}/__fake/put_side_effects`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ collect_clicks: false }) })
  await addOffer('Flexacil', 24800)
  await card().getByRole('button', { name: 'Push to KT' }).click()
  await confirmDialog('Push')
  await card().locator('[data-testid="side-effect"]').waitFor()
  assert.match(await card().locator('[data-testid="side-effect"]').innerText(), /collect_clicks/)
  assert.equal(await card().locator('.alert.ok').count(), 0) // нет зелёного успеха
  await fetch(`${FAKE}/__fake/put_side_effects`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  await row(24800).getByRole('button', { name: 'Remove' }).click()
  await row(24800).getByRole('button', { name: 'Bring back' }).waitFor()
  assert.ok(await card().getByRole('button', { name: 'Push to KT' }).isDisabled())
  await shot('07-side-effect')
  await page.getByRole('button', { name: 'Fetch streams from KT' }).click()
  await confirmDialog('Заменить данными Keitaro')
  await card().locator('[data-testid="side-effect"]').waitFor({ state: 'detached' })
})

await step('Кнопки видны в 1280×800', async () => {
  for (const name of ['Push to KT', 'Cancel']) {
    const box = await card().getByRole('button', { name }).boundingBox()
    assert.ok(box && box.x >= 0 && box.x + box.width <= 1280, `${name} вне экрана`)
  }
  for (const b of await card().locator('td.actions button').all()) {
    const box = await b.boundingBox()
    assert.ok(box && box.x + box.width <= 1280)
  }
})

await step('Создание кампании: двойной клик → одна кампания, оба потока, переход в редактор', async () => {
  await page.getByRole('button', { name: 'Новая кампания' }).click()
  await page.locator('#c-name').fill('ui-e2e')
  await page.locator('#c-geo').fill('mx, au')
  await page.locator('#c-offer').fill('Cardio')
  await page.locator('section.narrow .search-list li:has-text("10184")').click()
  await page.locator('#c-group').selectOption('9799')
  await shot('05-create-form')
  const btn = page.getByRole('button', { name: 'Create' })
  await btn.dblclick()
  await page.locator('[data-testid="job-result"]:has-text("создана")').waitFor()
  const all = await (await fetch(`${FAKE}/admin_api/v1/campaigns`, { headers: { 'Api-Key': 'fake-key' } })).json()
  assert.equal(all.filter((c) => c.name === 'test_simonov_ui-e2e').length, 1)
  await shot('06-created')
  await page.getByRole('button', { name: 'Открыть в редакторе' }).click()
  await page.locator('h2:has-text("test_simonov_ui-e2e")').waitFor()
  const text = await page.locator('main').innerText()
  assert.match(text, /test_simonov_ui-e2e/)
  assert.match(text, /Поток без офферов/)
  assert.match(text, /10184/)
})

await step('Валидация GEO на форме', async () => {
  await page.getByRole('button', { name: 'Новая кампания' }).click()
  await page.locator('#c-geo').fill('USA')
  assert.ok(await page.getByRole('button', { name: 'Create' }).isDisabled())
})

await step('Списки поиска закрыты после выбора', async () => {
  assert.equal(await page.locator('.search-list:visible').count(), 0)
})

assert.deepEqual(consoleErrors, [], 'ошибки в консоли браузера')
assert.ok(!httpLog.some((l) => l.startsWith('5')), 'ответы 5xx: ' + httpLog.join(', '))
console.log('Ожидаемые 4xx за прогон:', httpLog.join('; ') || 'нет')
console.log('E2E OK, JS errors: 0, 5xx: 0')
await browser.close()
