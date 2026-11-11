import os
import subprocess
import platform
import sys

BASE_FILE = "base.txt"
RESULT_FILE = "result.txt"

def init_files():
    """Проверяет наличие файлов base.txt и result.txt, если их нет — создает."""
    if not os.path.exists(BASE_FILE):
        with open(BASE_FILE, "w", encoding="utf-8") as f:
            f.write("66.86.15.0\n")
            f.write("66.86.0.0\n")
        print(f"[+] Создан файл {BASE_FILE} с примерами.")
    
    if not os.path.exists(RESULT_FILE):
        open(RESULT_FILE, "w", encoding="utf-8").close()
        print(f"[+] Создан файл {RESULT_FILE}.")

def ping_ip(ip):
    """Функция проверки пинга IP-адреса для Linux."""
    # -c 1: один пакет, -W 1: таймаут 1 секунда
    command = ["ping", "-c", "1", "-W", "1", ip]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except Exception:
        return False

def print_progress(current, total, prefix='', length=30):
    """Выводит анимированную полоску прогресса в консоль."""
    if total == 0:
        return
    percent = int(100 * current / total)
    filled = int(length * current // total)
    bar = '█' * filled + '-' * (length - filled)
    sys.stdout.write(f'\r{prefix} |{bar}| {percent}% ({current}/{total})')
    sys.stdout.flush()
    if current == total:
        sys.stdout.write('\n')

def parse_base_line(line):
    """Разбивает строку из base.txt на базовый IP и состояние."""
    parts = line.strip().split()
    if not parts:
        return None, None
    ip_base = parts[0]
    state = parts[1] if len(parts) > 1 else None
    return ip_base, state

def main():
    init_files()
    
    with open(BASE_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    updated_lines = []
    
    for line in lines:
        ip_base, state = parse_base_line(line)
        if not ip_base:
            continue
        
        octets = ip_base.split('.')
        if len(octets) != 4:
            continue
        
        print(f"\n[*] Сканирование подсети: {ip_base}")
        
        if ip_base.endswith('.0.0'):
            # Диапазон в двух октетах (65536 адресов)
            prefix = f"{octets[0]}.{octets[1]}"
            if not state:
                s3, s4 = 0, 0
            else:
                sp = state.split('.')
                s3 = int(sp[0]) if len(sp) > 0 else 0
                s4 = int(sp[1]) if len(sp) > 1 else 0
            
            total_ips = 256 * 256
            current_index = s3 * 256 + s4
            
            current_state = f"{s3}.{s4}"
            for i in range(s3, 256):
                j_start = s4 if i == s3 else 0
                for j in range(j_start, 256):
                    target_ip = f"{prefix}.{i}.{j}"
                    alive = ping_ip(target_ip)
                    res_char = 'y' if alive else 'n'
                    
                    with open(RESULT_FILE, "a", encoding="utf-8") as rf:
                        rf.write(f"{target_ip} {res_char}\n")
                    
                    current_state = f"{i}.{j}"
                    current_index += 1
                    print_progress(current_index, total_ips, prefix="Прогресс")
            
            updated_lines.append(f"{ip_base} {current_state}\n")

        elif ip_base.endswith('.0'):
            # Диапазон в последнем октете (256 адресов)
            prefix = f"{octets[0]}.{octets[1]}.{octets[2]}"
            start = int(state) if state is not None and state.isdigit() else 0
            
            total_ips = 256
            current_index = start
            
            current_state = str(start)
            for i in range(start, 256):
                target_ip = f"{prefix}.{i}"
                alive = ping_ip(target_ip)
                res_char = 'y' if alive else 'n'
                
                with open(RESULT_FILE, "a", encoding="utf-8") as rf:
                    rf.write(f"{target_ip} {res_char}\n")
                
                current_state = str(i)
                current_index += 1
                print_progress(current_index, total_ips, prefix="Прогресс")
            
            updated_lines.append(f"{ip_base} {current_state}\n")
            
        else:
            # Одиночный IP адрес
            alive = ping_ip(ip_base)
            res_char = 'y' if alive else 'n'
            with open(RESULT_FILE, "a", encoding="utf-8") as rf:
                rf.write(f"{ip_base} {res_char}\n")
            updated_lines.append(f"{ip_base} 1\n")
            print(f"[+] Одиночный IP {ip_base}: {res_char}")

    # Перезаписываем base.txt с обновленными данными о прогрессе
    with open(BASE_FILE, "w", encoding="utf-8") as f:
        f.writelines(updated_lines)
        
    print("\n[+] Проверка завершена. Все результаты сохранены в result.txt.")

if __name__ == "__main__":
    main()
