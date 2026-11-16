# Messenger — микросервисный мессенджер на Kubernetes

Учебное веб-приложение: регистрация и вход, личные диалоги, сохранение сообщений,
WebSocket-доставка и восстановление истории после переподключения.

**Компоненты:** Auth, Chat, Delivery, Outbox, Consumer, HAProxy, PostgreSQL,
Redis и RabbitMQ. Для сетевой сегментации используются Kubernetes NetworkPolicy
и Calico (запрет по умолчанию, отдельные разрешения для сервисов).

Секреты, дампы БД и локальные ключи в репозиторий не включены.

## Развёртывание в локальном Kubernetes

Необходимы Docker Desktop, `kind`, `kubectl` и Python 3.12.
Кластер создаётся отдельно от других проектов; его имя `messenger-step5`.

```bash
python3 -m pip install -r requirements-step5.txt
kind create cluster --name messenger-step5 --config infra/step5/kind.yaml
kubectl --context kind-messenger-step5 create -f https://raw.githubusercontent.com/projectcalico/calico/v3.33.0/manifests/v3_projectcalico_org.yaml
kubectl --context kind-messenger-step5 create -f https://raw.githubusercontent.com/projectcalico/calico/v3.33.0/manifests/tigera-operator.yaml
kubectl --context kind-messenger-step5 wait --for=condition=Established crd/installations.operator.tigera.io --timeout=120s
kubectl --context kind-messenger-step5 apply -f infra/step5/calico.yaml
kubectl --context kind-messenger-step5 wait --for=condition=Ready node --all --timeout=300s
python3 tools/deploy_step5.py --build --tag step5
```

Откройте http://127.0.0.1:8000 и зарегистрируйте двух пользователей
(можно в обычном и приватном окне браузера).

## Проверка

```bash
kubectl --context kind-messenger-step5 -n messenger get deployments,statefulsets,pods
kubectl --context kind-messenger-step5 -n messenger get networkpolicy
```

## Остановка и запуск после развёртывания кластера
Инструкция предназначена для компьютера, на котором уже развёрнут Kubernetes-кластер
messenger-step5 и установлено приложение. Повторная сборка образов
и установка Calico не требуются.

## Остановка
Остановить микросервисы и Redis:
```bash
kubectl --context kind-messenger-step5 -n messenger scale deployment auth chat delivery outbox consumer proxy redis --replicas=0
```

Остановить PostgreSQL и RabbitMQ:
```bash
kubectl --context kind-messenger-step5 -n messenger scale statefulset postgres rabbitmq --replicas=0
```
Данные в PersistentVolumeClaim (PVC), Kubernetes Secrets и NetworkPolicy сохраняются.

## Запуск
Убедиться, что Docker Desktop запущен и кластер существует:
```bash
kind get clusters
```

Запустить инфраструктуру:
```bash
kubectl --context kind-messenger-step5 -n messenger scale statefulset postgres rabbitmq --replicas=1
kubectl --context kind-messenger-step5 -n messenger scale deployment redis --replicas=1
```

Запустить прикладные микросервисы:
```bash
kubectl --context kind-messenger-step5 -n messenger scale deployment auth chat delivery outbox consumer proxy --replicas=1
```

Проверка работоспособности:
```bash
kubectl --context kind-messenger-step5 -n messenger get pods
curl http://127.0.0.1:8000/hello
```

Все девять основных Pod должны перейти в состояние Running с готовностью 1/1.
Веб-интерфейс приложения: **http://127.0.0.1:8000**

Для демонстрации сетевых разрешений и запретов доступны
`tools/check_step5.py` и `tools/check_step4.py`.
Дополнительные Python-зависимости инструментов проверки устанавливаются
из `requirements-test.txt`. Матрица сетевых взаимодействий находится
в `docs/step5/access-matrix.md`.
