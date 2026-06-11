<div align="center">

# Zapret Zen - утилита для быстрого обхода блокировок

<picture>
  <img alt="Zapret-Zen banner" src="assets/Hello.png">
</picture>

</div>

## Запуск
```
python -m venv venv
venv\Scripts\Activate.ps1
pip install -e .[dev]
python -m zapret_zen.main
```

## Сборка
```
venv\Scripts\python.exe -m PyInstaller -y packaging\zapret_zen.spec
```
