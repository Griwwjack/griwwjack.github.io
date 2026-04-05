# =============================================================================
# PROJETO: ABnetSim - Simulador de Onda de Sorter
# VERSÃO: 0.2
# AUTOR: Jurimar Mendes
# DATA: Abril / 2026
# DESCRIÇÃO: Simulador de fluxo de logística para integração com sistema ABnet.
#            Cria e atualiza o arquivo 'dados_abnet.txt' automaticamente.
# TECNOLOGIAS: Python 3, CustomTkinter (UI), Threading, Random.
# =============================================================================

import random
import time
import threading
import os
from datetime import datetime
import tkinter as tk
import customtkinter as ctk 

# Configurações de Tema
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class SimuladorABnet:
    def __init__(self, root):
        self.root = root
        self.root.title("ABnetSim v0.2")
        self.root.geometry("380x745") 
        self.root.resizable(False, False)
        
        # GARANTIR EXISTÊNCIA DO ARQUIVO NA INICIALIZAÇÃO
        self.verificar_arquivo_dados()
        
        self.dados_pedidos = []
        self.simulando = False
        self.faltas_aplicadas = False
        self.labels_color = "#d4d4d4"
        self.bg_progress = "#1a1a1a" 
        self.blue_progress = "#0078d4" 

        self.vcmd_int = (self.root.register(self.validar_inteiros), '%P')
        self.vcmd_float = (self.root.register(self.validar_decimal), '%P')

        # --- CONTAINER PRINCIPAL ---
        self.main_container = ctk.CTkFrame(master=self.root, fg_color="transparent")
        self.main_container.pack(expand=True, fill="both", padx=30, pady=(20, 10))

        # --- CABEÇALHO DA UI ---
        self.header_label = ctk.CTkLabel(master=self.main_container, text="Sorter - Simulador de Onda", 
                                         font=("Segoe UI", 24, "bold"), text_color="#ffffff")
        self.header_label.pack(anchor="w", pady=(0, 15))

        def criar_campo_moderno(label_text, default_val, tipo="texto"):
            frame = ctk.CTkFrame(master=self.main_container, fg_color="transparent")
            frame.pack(fill="x", pady=6)
            label = ctk.CTkLabel(master=frame, text=label_text, font=("Segoe UI", 11, "bold"), text_color=self.labels_color)
            label.pack(anchor="w", pady=(0, 2))
            var = tk.StringVar(value=default_val)
            
            extra_params = {}
            if tipo == "inteiro":
                extra_params = {"validate": "key", "validatecommand": self.vcmd_int}
            elif tipo == "decimal":
                extra_params = {"validate": "key", "validatecommand": self.vcmd_float}

            entry = ctk.CTkEntry(master=frame, textvariable=var, height=40, corner_radius=8,
                                 border_color="#2a2a2a", fg_color="#1a1a1a", justify="center",
                                 **extra_params)
            entry.pack(fill="x")
            return var

        # --- CAMPOS DE ENTRADA ---
        self.val_qtd = criar_campo_moderno("CHUTES ATIVOS", "10", "inteiro")
        self.val_total_global = criar_campo_moderno("TOTAL DE CAIXAS", "1000", "inteiro")
        self.val_faltas = criar_campo_moderno("FALTAS SIMULADAS", "0", "inteiro")
        self.val_minutos = criar_campo_moderno("DURAÇÃO DA ONDA (MIN)", "1", "decimal")
        self.val_fechamento_total_seg = criar_campo_moderno("DURAÇÃO DO FECHAMENTO DE CHUTES (SEG)", "0", "decimal")

        # --- PROGRESSO ---
        self.progress = ctk.CTkProgressBar(master=self.main_container, height=12, corner_radius=6,
                                           fg_color=self.bg_progress, progress_color=self.bg_progress)
        self.progress.pack(fill="x", pady=(20, 25))
        self.progress.set(0)

        # --- BOTÕES ---
        self.btn_gerar = ctk.CTkButton(master=self.main_container, text="Associar Onda", 
                                       command=lambda: [self.main_container.focus(), self.preparar_onda()],
                                       height=45, corner_radius=22, fg_color="#383838")
        self.btn_gerar.pack(fill="x", pady=4)

        self.btn_iniciar = ctk.CTkButton(master=self.main_container, text="Jogar Caixas", 
                                         command=lambda: [self.main_container.focus(), self.iniciar_thread_simulacao()],
                                         height=45, corner_radius=22, fg_color="#383838", state="disabled")
        self.btn_iniciar.pack(fill="x", pady=4)

        self.btn_fechar = ctk.CTkButton(master=self.main_container, text="Fechar Chutes", 
                                        command=lambda: [self.main_container.focus(), self.iniciar_thread_fechamento()],
                                         height=45, corner_radius=22, fg_color="#383838", state="disabled")
        self.btn_fechar.pack(fill="x", pady=4)

        # --- STATUS BAR ---
        self.status_bar = ctk.CTkFrame(master=self.root, height=35, fg_color="#111111", corner_radius=0)
        self.status_bar.pack(side="bottom", fill="x")
        self.status_label = ctk.CTkLabel(master=self.status_bar, text="> Sorter pronto", 
                                         font=("Segoe UI", 9, "bold"), text_color="#d4d4d4")
        self.status_label.pack(side="left", padx=20)

    def verificar_arquivo_dados(self):
        if not os.path.exists("dados_abnet.txt"):
            try:
                with open("dados_abnet.txt", "w", encoding="utf-8") as f:
                    f.write("")
            except Exception as e:
                print(f"Erro ao criar arquivo inicial: {e}")

    def validar_inteiros(self, p_value):
        return p_value == "" or p_value.isdigit()

    def validar_decimal(self, p_value):
        if p_value == "": return True
        try:
            val = float(p_value.replace(",", "."))
            return val >= 0
        except ValueError: return False

    def preparar_onda(self):
        try:
            qtd_v = self.val_qtd.get()
            total_v = self.val_total_global.get()
            qtd = int(qtd_v) if qtd_v else 0
            total_global = int(total_v) if total_v else 0
            
            if qtd <= 0 or total_global <= 0:
                self.status_label.configure(text="> CHUTES ATIVOS e TOTAL DE CAIXAS devem ser maiores que 0")
                return

            self.dados_pedidos = []
            self.faltas_aplicadas = False
            
            media, restante = total_global // qtd, total_global
            for i in range(1, qtd + 1):
                cota = restante if i == qtd else random.randint(int(media*0.8), int(media*1.2))
                if cota > restante - (qtd - i): cota = restante - (qtd - i)
                restante -= cota
                self.dados_pedidos.append({
                    "id_linha": i, "pedido": random.randint(2000000, 2100000), "chute": 2 + i - 1,
                    "total_items": cota, "transf_restante": cota, 
                    "classificados": 0, "em_caminho": 0, "falta_neste_chute": 0
                })
            self.atualizar_arquivo()
            self.progress.set(0)
            self.progress.configure(progress_color=self.bg_progress)
            self.btn_iniciar.configure(state="normal")
            self.status_label.configure(text=f"> Onda Associada: {qtd} Chutes Ativos  -  {total_global} Caixas", text_color="#d4d4d4")
        except: pass

    def atualizar_arquivo(self):
        agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        st = 'style="Background-Color:;color:;font-weight:;font-style:;font-size:;font-family:;"'
        html_topo = f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>WMS</title></head><body><form id="SorterOrders">
<div class="universe"><h2>Data: {agora}</h2><table border="1">
<tr {st}><th class="text">Pedido</th><th class="text">Chute</th><th class="text">Total de items</th><th class="text">items clasificados</th><th class="text">Em caminho</th></tr>"""
        corpo = ""
        for i, p in enumerate(self.dados_pedidos, start=1):
            corpo += f'''<tr {st}><td class="text" {st}><input name="S_56_{i}_id" type="hidden" value="{p['pedido']}" />{p['pedido']}</td><td class="text" {st}><input name="S_56_{i}_ch" type="hidden" value="{p['chute']}" />{p['chute']}</td><td class="text" {st}>{p['total_items']}</td><td class="text" {st}>{p['classificados']}</td><td class="text" {st}>{p['em_caminho']}</td></tr>'''
        html_fim = "</table></div></form></body></html>"
        try:
            with open("dados_abnet.txt", "w", encoding="utf-8") as f: f.write(html_topo + corpo + html_fim)
        except: pass

    def iniciar_thread_simulacao(self):
        self.btn_gerar.configure(state="disabled")
        self.btn_iniciar.configure(state="disabled")
        self.progress.configure(progress_color=self.blue_progress) 
        self.simulando = True
        threading.Thread(target=self.loop_simulacao, daemon=True).start()

    def loop_simulacao(self):
        try:
            minutos = float(self.val_minutos.get() or 0)
            tempo_total = minutos * 60
            inicio = time.time()
            total_onda_original = sum(p['total_items'] for p in self.dados_pedidos)
            faltas_total = int(self.val_faltas.get() or 0)
            
            while self.simulando:
                decorrido = time.time() - inicio
                progresso_percento = decorrido / tempo_total if tempo_total > 0 else 1.0
                if (decorrido >= tempo_total and tempo_total > 0) or (tempo_total == 0): break
                
                if progresso_percento >= 0.5 and not self.faltas_aplicadas:
                    faltas_distribuir = faltas_total
                    while faltas_distribuir > 0:
                        possiveis = [x for x in self.dados_pedidos if x['total_items'] > x['falta_neste_chute']]
                        if not possiveis: break
                        p = random.choice(possiveis)
                        p['falta_neste_chute'] += 1
                        if p['transf_restante'] > 0: p['transf_restante'] -= 1
                        faltas_distribuir -= 1
                    self.faltas_aplicadas = True

                meta = int(progresso_percento * total_onda_original)
                atual = sum(p['classificados'] + p['em_caminho'] for p in self.dados_pedidos)
                
                if atual < meta:
                    for _ in range(max(0, meta - atual)):
                        ativos = [p for p in self.dados_pedidos if p['transf_restante'] > 0]
                        if not ativos: break
                        p = random.choice(ativos)
                        lote = random.randint(1, min(p['transf_restante'], 5))
                        p['em_caminho'] += lote
                        p['transf_restante'] -= lote

                for p in self.dados_pedidos:
                    if p['em_caminho'] > 0:
                        vazao = random.randint(1, min(p['em_caminho'], 4))
                        p['em_caminho'] -= vazao
                        p['classificados'] += vazao
                
                self.progress.set(sum(p['classificados'] for p in self.dados_pedidos) / total_onda_original)
                self.atualizar_arquivo()
                if sum(p['classificados'] for p in self.dados_pedidos) >= (total_onda_original - faltas_total): break
                time.sleep(1)

            for p in self.dados_pedidos:
                p['classificados'] = p['total_items'] - p['falta_neste_chute']
                p['em_caminho'] = 0
            self.progress.set(1.0)
            self.atualizar_arquivo()
            self.root.after(0, self.fim_simulacao)
        except: pass

    def fim_simulacao(self):
        self.status_label.configure(text="> Separação Concluída.", text_color="#d4d4d4")
        self.btn_fechar.configure(state="normal")

    def iniciar_thread_fechamento(self):
        self.btn_fechar.configure(state="disabled")
        threading.Thread(target=self.loop_fechamento, daemon=True).start()

    def loop_fechamento(self):
        try:
            tempo_total_fechamento = float(self.val_fechamento_total_seg.get() or 0)
            total_chutes_original = len(self.dados_pedidos)
            
            if total_chutes_original > 0:
                intervalo = tempo_total_fechamento / total_chutes_original
            else:
                intervalo = 0

            while len(self.dados_pedidos) > 0:
                ch = self.dados_pedidos.pop(0)
                self.atualizar_arquivo()
                self.progress.set(len(self.dados_pedidos) / total_chutes_original)
                self.root.after(0, lambda c=ch['chute']: self.status_label.configure(text=f"> Fechando Chute {c}"))
                if intervalo > 0: time.sleep(intervalo)
            
            self.root.after(0, self.fim_fechamento)
        except: pass

    def fim_fechamento(self):
        self.progress.set(0)
        self.progress.configure(progress_color=self.bg_progress)
        self.btn_gerar.configure(state="normal")
        self.status_label.configure(text="> Chutes Fechados.", text_color="#d4d4d4")

if __name__ == "__main__":
    root = ctk.CTk()
    app = SimuladorABnet(root)
    root.mainloop()
