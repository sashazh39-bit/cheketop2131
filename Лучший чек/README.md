# Лучший чек

Чеки с «Филипп Юсаев Ч.»: заглавные буквы Ф, Ч, Ю. Размер 8.8–9.2 KB для прохождения проверки в TG.

**Эталон:** 13-03-26_00-00 13.pdf (9 240 bytes)  
**Донор букв:** check (3).pdf

## Варианты (check 3a — проходят проверку)

| Файл | Размер | Подход |
|------|--------|--------|
| check 3a.pdf | ~9 271 B | compact: deepcopy + transplant + sync /W |
| check 3a_compact.pdf | ~9 271 B | REPLACE deepcopy + transplant, глифы ФЧЮ из add, /W синхрон |
| check 3a_deepcopy.pdf | ~9 267 B | Как compact (deepcopy + transplant) |
| check 3a_transplant.pdf | ~9 271 B | compact + transplant (дубликат compact) |
| check 3a_decompose.pdf | ~9 299 B | REPLACE decompose + transplant (альт. метод) |
| check 3a_add.pdf | ~9 469 B | ADD: новые CIDs, размер больше |

**Визуал:** transplant копирует глифы Ф,Ч,Ю из add в REPLACE-чек. Sync /W — ширины из add для точной метрики.

## Генерация

```bash
python3 gen_checks.py --only 3a
python3 gen_checks.py --etalon /path/to/13.pdf
```
