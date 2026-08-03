# Сертификаты для MAX Bot API

`platform-api2.max.ru` использует цепочку **Russian Trusted CA** (Минцифры).
Её нет в корнях macOS и в Mozilla/certifi.

| Файл | Назначение |
|------|------------|
| `russian_trusted_ca_bundle.pem` | Root + Sub CA (использует код) |
| `russian_trusted_root_ca.cer` | Root (источник: gu-st.ru) |
| `russian_trusted_sub_ca.cer` | Sub CA |

Обновить с [gu-st.ru](https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer):

```bash
curl -fsSL -o russian_trusted_root_ca.cer https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer
curl -fsSL -o russian_trusted_sub_ca.cer https://gu-st.ru/content/Other/doc/russian_trusted_sub_ca.cer
# затем склеить в bundle без CRLF
```
