# Калибровка выравнивания чека ВТБ СБП

## Принцип

Последняя буква каждого поля правого столбца должна оказаться на одной вертикали (wall). Исключение: ФИО под заголовком «Исходящий перевод СБП» — центрируется.

Формула: `tm_x = wall - n * pts`, где `pts` — ширина одного глифа (зависит от керна и шрифта).

## Схема (гибрид)

1. **layout_config.json** — база из `python3 scan_donors_align.py` (163 донора)
2. **layout_overrides.json** — ручные поправки (применяются поверх)

---

## Шаги к идеальному выравниванию

### Шаг 1. Обновить базу из эталонов

Запустить скан по всем донорам в `база_чеков/vtb/СБП`:

```bash
python3 scan_donors_align.py
```

Результат: layout_config.json обновлён медианой pts, wall и Y по эталонным чекам.

### Шаг 2. Сгенерировать тестовый чек

```bash
python3 gen_random_receipt.py test.pdf
```

Счёт не меняется (account=None), остальные поля подставляются.

### Шаг 3. Получить отчёт выравнивания

```bash
python3 report_last_letter_coords.py test.pdf --tol 0.5
```

В таблице: поле, x1, Δ (отклонение от wall), статус (OK / съезд).

Или с визуальной линией по букве «о» в «Выполнено»:

```bash
python3 report_last_letter_coords.py test.pdf --visual-out test_line.pdf
```

Открыть test_line.pdf и проверить, какие поля уходят влево/вправо от красной линии.

### Шаг 4. Подправить pts в layout_overrides.json

| Поле | Текст уходит вправо | Текст уходит влево |
|------|---------------------|--------------------|
| date, account, phone, amount | Уменьшить pts | Увеличить pts |
| payer, recipient, bank | Увеличить pts | Уменьшить pts |

Редактировать layout_overrides.json, секция "pts". Изменить значение на 2–5%, сохранить, повторить Шаги 2–3.

### Шаг 5. Проверить Y (при необходимости)

Если какое‑то поле не обновляется или подменяется другим, поправить Y в layout_overrides.json, секция "y". Текущие Y — из эталонов. Менять только при явных ошибках.

### Шаг 6. Сверить с эталоном

```bash
python3 report_last_letter_coords.py "база_чеков/vtb/СБП/Александр Валерьевич М..pdf" --visual-out ideal_line.pdf
```

Открыть ideal_line.pdf — все поля эталона должны проходить по линии.

### Шаг 7. Итерация

1. Сгенерировать чек: `python3 gen_random_receipt.py test.pdf`
2. Отчёт: `python3 report_last_letter_coords.py test.pdf --visual-out test_line.pdf`
3. Проверить, у каких полей съезд
4. Обновить pts в layout_overrides.json по таблице из Шага 4
5. Повторить до достижения Δ < 0.5 pt по всем полям

---

## Краткая цепочка команд

```bash
python3 scan_donors_align.py
python3 gen_random_receipt.py test.pdf
python3 report_last_letter_coords.py test.pdf --visual-out test_line.pdf
# Просмотр test_line.pdf → правка layout_overrides.json → повторить
```

---

## Диагностика по полям

- **Выполнено**: не заменяется, wall берётся от него
- **ID операции**: исключён блок «Выполнено» (exclude_y_list), n_min=15
- **Счёт**: не подменяется (account=None в gen_random_receipt)
- **ФИО**: взаимное исключение payer/recipient по Y
- **Допуск**: --tol в отчёте задаёт порог «съезд»; для строгой проверки использовать 0.3–0.5
