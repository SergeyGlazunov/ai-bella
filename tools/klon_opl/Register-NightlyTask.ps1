# Создаёт/обновляет задачу Task Scheduler для ночной выгрузки klon_opl.
# Запуск от администратора не обязателен (LIMITED), но учётка должна иметь право Create.
schtasks /Create /TN "AI-contour klon_opl nightly export" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\AI-contour\tools\klon_opl\Export-KlonOpl.ps1" /SC DAILY /ST 02:00 /RL LIMITED /F
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
schtasks /Query /TN "AI-contour klon_opl nightly export" /FO LIST
