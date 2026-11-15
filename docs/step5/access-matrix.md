# Матрица доступа шага 5

Все соединения ниже инициируются слева направо. Сервисные потоки — TCP.
Ответный трафик разрешённого соединения не требует отдельного обратного разрешения.
Метки: `app.kubernetes.io/part-of=messenger`,
`app.kubernetes.io/component=<имя процесса>`. Chat/outbox/consumer имеют разные метки.

| Источник | Назначение | Порт | Обоснование |
|---|---|---|---|
| Внешний клиент | proxy | 8000 | HTTP/WS, публикация только 127.0.0.1 |
| proxy | auth | 8000 | Auth API, профили, health |
| proxy | chat | 8000 | HTML, API, health |
| proxy | delivery | 8000 | WebSocket, health |
| chat | auth | 8000 | Внутренний API профилей |
| chat | postgres | 5432 | chat_db |
| chat | redis | 6379 | Кэш |
| auth | postgres | 5432 | auth_db |
| outbox | postgres | 5432 | Outbox |
| outbox | redis | 6379 | Инвалидация |
| outbox | rabbitmq | 5672 | Публикация |
| delivery | rabbitmq | 5672 | Подписка |
| consumer | rabbitmq | 5672 | Журналирование |
| migrate | postgres | 5432 | SQL Job |
| proxy/auth/chat/delivery/outbox/consumer/migrate | CoreDNS в kube-system | UDP/TCP 53 | DNS |

Остальные обычные межподовые соединения блокируются default-deny в обоих направлениях.
PostgreSQL/Redis/RabbitMQ не получают разрешения инициировать внешние подключения.
Для одиночного RabbitMQ фиксирован nodename `rabbit@rabbitmq-0`; кластерные AMQP/Erlang
порты не открываются. Панель управления 15672 не является разрешённым рабочим потоком.

Контрольные запреты: delivery → postgres/redis, auth → redis/rabbitmq,
consumer → postgres, chat → rabbitmq; pod без роли → postgres.
Проверки по Service и Pod IP исполняются из настоящих подов (Python socket).
Proxy → backend проверяется сквозной HTTP/WS-приёмкой; прямой отрицательный
proxy → PostgreSQL статически покрыт матрицей, отдельный exec TCP-probe из образа
HAProxy не реализован. Job → PostgreSQL подтверждается успешной миграцией.
