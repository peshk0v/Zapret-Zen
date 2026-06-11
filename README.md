<div align="center">

# Zapret Zen - утилита для быстрого обхода блокировок

<picture>
  <img alt="Zapret-Zen banner" src="assets/Hello.png">
</picture>

## Темы
### Светлые
<img width="860" height="520" alt="lighttheme" src="https://github.com/user-attachments/assets/be27b57c-0071-466f-9793-43e7cbed715b" />

### Темные
<img width="860" height="520" alt="darktheme" src="https://github.com/user-attachments/assets/e67f7f01-6dfc-454f-b032-d62a77ed2789" />

</div>

## Разработчикам
### Запуск
```
python -m venv venv
venv\Scripts\Activate.ps1
pip install -e .[dev]
python -m zapret_zen.main
```
### Сборка
```
venv\Scripts\python.exe -m PyInstaller -y packaging\zapret_zen.spec
```
