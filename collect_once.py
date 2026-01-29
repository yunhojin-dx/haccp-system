import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import pandas as pd
import numpy as np
from datetime import datetime
import os
import shutil
import subprocess
import json

class PUMasterSystem:
    def __init__(self, root):
        self.root = root
        self.root.title("지평주조 PU 통합 관리 시스템 v3.0 (Lite)")
        self.root.geometry("1000x700") # 불필요한 공간 줄임
        
        # --- 폴더 자동 생성 ---
        self.base_dir = os.getcwd()
        self.archive_dir = os.path.join(self.base_dir, "DATA_ARCHIVE")
        self.result_dir = os.path.join(self.base_dir, "RESULT_LOGS")
        self.config_file = os.path.join(self.base_dir, "config.json")
        
        if not os.path.exists(self.archive_dir): os.makedirs(self.archive_dir)
        if not os.path.exists(self.result_dir): os.makedirs(self.result_dir)

        # --- 변수 초기화 ---
        self.top_files = []
        self.bottom_files = []
        self.time_ranges = []
        
        # --- 탭 구성 ---
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # 탭 1: 분석 및 등록
        self.tab1 = tk.Frame(self.notebook)
        self.notebook.add(self.tab1, text="  [1] PU 분석 및 데이터 등록  ")
        self.setup_analysis_tab()

        # 탭 2: 이력 조회
        self.tab2 = tk.Frame(self.notebook)
        self.notebook.add(self.tab2, text="  [2] 과거 이력 조회  ")
        self.setup_search_tab()

        # --- 설정 불러오기 ---
        self.load_config()

    # =========================================================
    # TAB 1: 분석 및 등록 기능
    # =========================================================
    def setup_analysis_tab(self):
        # 레이아웃: 좌측(설정) / 우측(로그)
        paned_window = tk.PanedWindow(self.tab1, orient=tk.HORIZONTAL)
        paned_window.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        left_frame = tk.Frame(paned_window, padx=10, pady=10, width=400)
        right_frame = tk.Frame(paned_window, padx=10, pady=10)
        
        paned_window.add(left_frame)
        paned_window.add(right_frame)

        # --- [좌측] 설정 및 실행 ---
        # 1. 시간 설정
        tk.Label(left_frame, text="1. 살균 시간 설정", font=("Malgun Gothic", 12, "bold")).pack(anchor="w", pady=5)
        t_frame = tk.Frame(left_frame)
        t_frame.pack(anchor="w")
        
        self.start_entry = tk.Entry(t_frame, width=8); self.start_entry.insert(0, "00:00:00")
        self.start_entry.pack(side=tk.LEFT, padx=2)
        tk.Label(t_frame, text="~").pack(side=tk.LEFT)
        self.end_entry = tk.Entry(t_frame, width=8); self.end_entry.insert(0, "00:00:00")
        self.end_entry.pack(side=tk.LEFT, padx=2)
        tk.Button(t_frame, text="추가", command=self.add_time, bg="#eee").pack(side=tk.LEFT, padx=2)
        
        self.time_list = tk.Listbox(left_frame, height=4)
        self.time_list.pack(fill=tk.X, pady=5)
        
        btn_del_frame = tk.Frame(left_frame)
        btn_del_frame.pack(fill=tk.X)
        tk.Button(btn_del_frame, text="선택 삭제", command=self.del_time, fg="red").pack(side=tk.RIGHT)
        tk.Button(btn_del_frame, text="설정 저장", command=self.save_config, fg="blue").pack(side=tk.LEFT)

        tk.Frame(left_frame, height=2, bd=1, relief=tk.SUNKEN).pack(fill=tk.X, pady=15)

        # 2. 파일 선택
        tk.Label(left_frame, text="2. 데이터 파일 선택", font=("Malgun Gothic", 12, "bold")).pack(anchor="w", pady=5)
        
        tk.Label(left_frame, text="상층부 (Top):").pack(anchor="w")
        self.btn_top = tk.Button(left_frame, text="📂 상층 파일 열기", command=self.sel_top)
        self.btn_top.pack(fill=tk.X)
        self.lbl_top = tk.Label(left_frame, text="0개 선택됨", fg="blue")
        self.lbl_top.pack(anchor="w")

        tk.Label(left_frame, text="하층부 (Bottom):").pack(anchor="w", pady=(5,0))
        self.btn_bot = tk.Button(left_frame, text="📂 하층 파일 열기", command=self.sel_bot)
        self.btn_bot.pack(fill=tk.X)
        self.lbl_bot = tk.Label(left_frame, text="0개 선택됨", fg="blue")
        self.lbl_bot.pack(anchor="w")

        tk.Frame(left_frame, height=2, bd=1, relief=tk.SUNKEN).pack(fill=tk.X, pady=20)

        # 3. 실행 버튼
        self.btn_run = tk.Button(left_frame, text="▶ 분석 실행 및 저장", command=self.run_analysis, 
                                 bg="navy", fg="white", font=("Malgun Gothic", 14, "bold"), height=2)
        self.btn_run.pack(fill=tk.X)
        tk.Label(left_frame, text="※ 원본은 'DATA_ARCHIVE'에,\n결과는 'RESULT_LOGS'에 저장됨.", 
                 fg="gray", justify=tk.LEFT).pack(pady=10)

        # --- [우측] 로그 ---
        tk.Label(right_frame, text="실시간 분석 로그", font=("Malgun Gothic", 10, "bold")).pack(anchor="w")
        self.log_area = scrolledtext.ScrolledText(right_frame, state='disabled', font=("Consolas", 10))
        self.log_area.pack(fill=tk.BOTH, expand=True)

    # =========================================================
    # TAB 2: 과거 이력 조회 기능
    # =========================================================
    def setup_search_tab(self):
        top_frame = tk.Frame(self.tab2, padx=10, pady=10)
        top_frame.pack(fill=tk.X)
        
        center_frame = tk.Frame(self.tab2, padx=10, pady=10)
        center_frame.pack(fill=tk.BOTH, expand=True)

        # 검색 입력
        tk.Label(top_frame, text="조회할 날짜 (YYYY-MM-DD): ", font=("Malgun Gothic", 12)).pack(side=tk.LEFT)
        self.search_entry = tk.Entry(top_frame, width=15, font=("Malgun Gothic", 12))
        self.search_entry.pack(side=tk.LEFT, padx=5)
        self.search_entry.insert(0, datetime.now().strftime("%Y-%m-%d"))

        tk.Button(top_frame, text="🔍 조회하기", command=self.search_history, bg="darkgreen", fg="white", font=("Malgun Gothic", 10)).pack(side=tk.LEFT, padx=10)
        tk.Button(top_frame, text="📂 해당 폴더 열기", command=self.open_archive_folder, bg="#eee").pack(side=tk.RIGHT)

        # 결과 테이블
        columns = ("시간", "위치", "PU값", "판정", "최고온도", "최저온도", "파일명")
        self.tree = ttk.Treeview(center_frame, columns=columns, show="headings")
        
        self.tree.heading("시간", text="분석 시간")
        self.tree.column("시간", width=100)
        self.tree.heading("위치", text="위치")
        self.tree.column("위치", width=80)
        self.tree.heading("PU값", text="PU값")
        self.tree.column("PU값", width=80)
        self.tree.heading("판정", text="판정")
        self.tree.column("판정", width=80)
        self.tree.heading("최고온도", text="최고(Max)")
        self.tree.column("최고온도", width=80)
        self.tree.heading("최저온도", text="최저(Min)")
        self.tree.column("최저온도", width=80)
        self.tree.heading("파일명", text="파일명")
        self.tree.column("파일명", width=300)
        
        self.tree.pack(fill=tk.BOTH, expand=True)

    # --- 설정 저장/불러오기 ---
    def save_config(self):
        config = {"time_ranges": self.time_ranges}
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4)
            messagebox.showinfo("저장", "현재 시간 설정이 저장되었습니다.")
        except Exception as e:
            messagebox.showerror("오류", str(e))

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.time_ranges = config.get("time_ranges", [])
                    for s, e in self.time_ranges:
                        self.time_list.insert(tk.END, f"{s} ~ {e}")
            except:
                pass

    # --- TAB 1 기능 함수들 ---
    def add_time(self):
        s, e = self.start_entry.get(), self.end_entry.get()
        if len(s)<5 or len(e)<5: return
        self.time_ranges.append((s, e))
        self.time_list.insert(tk.END, f"{s} ~ {e}")

    def del_time(self):
        sel = self.time_list.curselection()
        if sel: 
            del self.time_ranges[sel[0]]
            self.time_list.delete(sel[0])

    def sel_top(self):
        fs = filedialog.askopenfilenames(filetypes=[("Excel", "*.xlsx *.xls *.csv")])
        if fs: self.top_files = fs; self.lbl_top.config(text=f"{len(fs)}개")

    def sel_bot(self):
        fs = filedialog.askopenfilenames(filetypes=[("Excel", "*.xlsx *.xls *.csv")])
        if fs: self.bottom_files = fs; self.lbl_bot.config(text=f"{len(fs)}개")

    def log(self, txt):
        self.log_area.config(state='normal')
        self.log_area.insert(tk.END, txt+"\n")
        self.log_area.see(tk.END)
        self.log_area.config(state='disabled')

    # --- 분석 및 저장 로직 (온도 보정 적용) ---
    def run_analysis(self):
        self.log_area.config(state='normal'); self.log_area.delete(1.0, tk.END); self.log_area.config(state='disabled')
        if not self.time_ranges or (not self.top_files and not self.bottom_files):
            messagebox.showwarning("경고", "시간과 파일을 확인해주세요.")
            return

        today_str = datetime.now().strftime("%Y-%m-%d")
        daily_archive_path = os.path.join(self.archive_dir, today_str)
        if not os.path.exists(daily_archive_path):
            os.makedirs(daily_archive_path)

        self.log(f"=== 분석 시작: {today_str} ===")
        save_data = [] 
        
        def process(files, layer_name):
            if not files: return
            
            for fpath in files:
                try:
                    # 1. 파일 복사
                    fname = os.path.basename(fpath)
                    timestamp_fname = f"{datetime.now().strftime('%H%M%S')}_{fname}"
                    dest_path = os.path.join(daily_archive_path, timestamp_fname)
                    shutil.copy2(fpath, dest_path)
                    
                    # 2. 데이터 로드
                    if fpath.endswith('.csv'):
                        try: df = pd.read_csv(fpath, skiprows=6)
                        except: df = pd.read_csv(fpath, encoding='cp949', skiprows=6)
                    else:
                        tdf = pd.read_excel(fpath, sheet_name=1, header=None)
                        sr = 0
                        for i, r in tdf.iterrows():
                            if '날짜' in r.values: sr=i; break
                        df = pd.read_excel(fpath, sheet_name=1, header=sr)
                    
                    df.columns = [str(c).strip() for c in df.columns]
                    temp_col = [c for c in df.columns if '온도' in c][0]
                    df['TS'] = df.apply(lambda r: pd.to_datetime(f"{r['날짜']} {r['시간']}"), axis=1)

                    # 3. 구간 분석
                    val = 0
                    temps_for_stats = []
                    
                    for s, e in self.time_ranges:
                        fd = df['날짜'].iloc[0]
                        s_dt = pd.to_datetime(f"{fd} {s}")
                        e_dt = pd.to_datetime(f"{fd} {e}")
                        
                        mask = (df['TS']>=s_dt) & (df['TS']<=e_dt)
                        filtered = df.loc[mask]
                        
                        if not filtered.empty:
                            temps = filtered[temp_col].tolist()
                            
                            # ★ [핵심 기능] 온도 보정 로직 적용 ★
                            # 만약 엑셀 파일에 '255'(25.5도) 또는 '650'(65.0도)처럼 정수로 들어있다면
                            # 자동으로 10으로 나누어 정상 온도로 변환합니다.
                            corrected_temps = []
                            for t in temps:
                                # 100도 이상이면 10으로 나눔 (예: 650 -> 65.0)
                                if t > 100: 
                                    t = t / 10.0
                                corrected_temps.append(t)
                                
                            temps_for_stats.extend(corrected_temps) # 통계용
                            
                            # PU 계산
                            for t in corrected_temps:
                                if t>=50: val += 1 * (1.393**(t-60))
                    
                    val = round(val, 2)
                    max_t = round(max(temps_for_stats), 1) if temps_for_stats else 0
                    min_t = round(min(temps_for_stats), 1) if temps_for_stats else 0
                    
                    status = "정상"
                    if val < 10: status = "부족"
                    elif val > 50: status = "과잉"
                    
                    self.log(f"[{layer_name}] {fname}")
                    self.log(f"  └ PU: {val} / Max: {max_t}℃ / Min: {min_t}℃ ({status})")
                    
                    # 4. 저장 데이터 구성
                    save_data.append({
                        "분석일자": today_str,
                        "분석시간": datetime.now().strftime("%H:%M:%S"),
                        "위치": layer_name,
                        "PU값": val,
                        "판정": status,
                        "최고온도": max_t,
                        "최저온도": min_t,
                        "파일명": timestamp_fname,
                        "원본파일명": fname
                    })

                except Exception as ex:
                    self.log(f"Error {fpath}: {ex}")

        process(self.top_files, "상층부")
        process(self.bottom_files, "하층부")

        # 엑셀 저장
        log_file = os.path.join(self.result_dir, "통합_분석_리포트.xlsx")
        new_df = pd.DataFrame(save_data)
        
        if os.path.exists(log_file):
            try:
                old_df = pd.read_excel(log_file)
                final_df = pd.concat([old_df, new_df], ignore_index=True)
                final_df.to_excel(log_file, index=False)
            except:
                self.log("❌ 엑셀 저장 실패 (파일이 열려있나요?)")
        else:
            new_df.to_excel(log_file, index=False)
            
        self.log("✅ 분석 및 저장 완료!")
        messagebox.showinfo("완료", "분석이 완료되었습니다.")
        
        # 탭2 자동 새로고침
        self.search_entry.delete(0, tk.END)
        self.search_entry.insert(0, today_str)
        self.search_history()

    # --- TAB 2 기능 함수들 ---
    def search_history(self):
        target_date = self.search_entry.get()
        log_file = os.path.join(self.result_dir, "통합_분석_리포트.xlsx")
        
        for item in self.tree.get_children():
            self.tree.delete(item)
            
        if not os.path.exists(log_file):
            messagebox.showinfo("알림", "아직 저장된 분석 기록이 없습니다.")
            return
            
        try:
            df = pd.read_excel(log_file)
            df['분석일자'] = df['분석일자'].astype(str)
            filtered = df[df['분석일자'] == target_date]
            
            if filtered.empty:
                messagebox.showinfo("알림", f"{target_date} 날짜의 기록이 없습니다.")
                return
                
            for i, row in filtered.iterrows():
                max_t = row['최고온도'] if '최고온도' in row else '-'
                min_t = row['최저온도'] if '최저온도' in row else '-'
                
                self.tree.insert("", "end", values=(
                    row['분석시간'], row['위치'], row['PU값'], row['판정'], 
                    max_t, min_t, row['파일명']
                ))
        except Exception as e:
            messagebox.showerror("에러", f"기록을 불러오는 중 오류 발생: {e}")

    def open_archive_folder(self):
        target_date = self.search_entry.get()
        target_path = os.path.join(self.archive_dir, target_date)
        
        if os.path.exists(target_path):
            subprocess.Popen(f'explorer "{os.path.abspath(target_path)}"')
        else:
            messagebox.showwarning("알림", f"{target_date} 날짜의 파일 폴더가 존재하지 않습니다.")

if __name__ == "__main__":
    root = tk.Tk()
    app = PUMasterSystem(root)
    root.mainloop()
