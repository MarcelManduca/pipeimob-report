import random
from datetime import datetime, timedelta

# Anonymous lookup tables for demo mode data (strictly synthetic, no real spreadsheet names)
MANAGERS = [
    "Corretor Alfa", "Corretor Beta", "Corretor Gama", "Corretor Delta", 
    "Corretor Epsilon", "Corretor Zeta", "Corretor Eta", "Corretor Theta", 
    "Corretor Iota", "Corretor Kappa"
]

BAIRROS = [
    "Bairro Centro", "Bairro Norte", "Bairro Sul", "Bairro Leste", "Bairro Oeste",
    "Zona Central", "Distrito Alpha", "Distrito Beta"
]

CATEGORIES = [
    "Apartamento Tipo", "Casa Residencial", "Sala Comercial", "Terreno Comercial", "Estúdio Residencial", "Cobertura Duplex"
]

ORIGINS = [
    "Canal de Atendimento A", "Canal de Atendimento B", "Portal Imobiliário A", 
    "Portal Imobiliário B", "Indicação Direta", "Mídias Sociais A", "Mídias Sociais B"
]

STAGES = ["Proposta", "Diligência", "Fechamento", "Arquivado"]

BANKS = ["Instituição Financeira A", "Instituição Financeira B", "Instituição Financeira C"]

def generate_mock_transactions():
    transactions = []
    start_date = datetime(2024, 1, 1)
    
    for i in range(1, 61):
        manager = MANAGERS[i % len(MANAGERS)]
        bairro = BAIRROS[i % len(BAIRROS)]
        category = CATEGORIES[i % len(CATEGORIES)]
        origin = ORIGINS[i % len(ORIGINS)]
        stage = STAGES[i % len(STAGES)]
        
        # Synthetic sales volumes ranging from R$ 100k to R$ 5M
        val_factor = (i * 9) % 37
        contract_val = float(200000 + val_factor * 120000)
        
        commission_rate = 0.05 + (float((i * 2) % 3) * 0.01)  # 5% to 7%
        total_commission = float(round(contract_val * commission_rate, 2))
        commission_imobiliaria = float(round(total_commission * 0.5, 2))
        commission_corretor = float(round(total_commission - commission_imobiliaria, 2))
        
        days_offset = (i * 14) % 850
        tx_datetime = start_date + timedelta(days=days_offset)
        tx_date_str = tx_datetime.strftime("%Y-%m-%d")
        
        financing = (i % 3) != 0
        bank = BANKS[i % len(BANKS)] if financing else None
        
        client_buyer = f"Cliente Comprador {i}"
        client_seller = f"Cliente Vendedor {i}"
        
        is_terceiro = (i % 2) != 0
        seller_origin = "Imóvel de Terceiro" if is_terceiro else "Lançamento Novo"
        agency_type = "Franquia Licenciada" if is_terceiro else "Parceria Construtora"
        
        forma_pgto = [
            {"nome": "Sinal Inicial", "valor": float(round(contract_val * 0.15, 2)), "detalhes": "Sinal via PIX"}
        ]
        if financing:
            forma_pgto.append({"nome": "Financiamento Bancário", "valor": float(round(contract_val * 0.85, 2)), "detalhes": f"Financiado com o {bank}"})
        else:
            forma_pgto.append({"nome": "Recursos Próprios", "valor": float(round(contract_val * 0.85, 2)), "detalhes": "TED Bancária"})

        tx = {
            "transacao_unique_id_pipeimob": f"tx_demo_{100 + i}",
            "codigo_contrato": f"CONTRATO-DEMO-{1000 + i}",
            "codigo_imovel": f"IMO-DEMO-{2000 + i}",
            "etapa_atual": stage,
            "data_contrato": tx_date_str,
            "data_inicio_venda": tx_date_str,
            "valor_contrato": contract_val,
            "total_comissao": total_commission,
            "comissao_imobiliaria": commission_imobiliaria,
            "agente_gestor": manager,
            "midia_origem_compradores": origin,
            "midia_origem_vendedores": seller_origin,
            "categoria_crm": category,
            "imobiliária": "Imobiliária Demonstrativa",
            "imobiliária_tipo": agency_type,
            "financiamento": financing,
            "financiamento_banco": bank,
            "endereco_bairro": bairro,
            "forma_pagamento": forma_pgto,
            "comissionados": [
                {"nome": "Imobiliária Demonstrativa", "tipo": "Empresa", "valor": commission_imobiliaria, "comissionado_imobiliaria": True, "comissionado_valor": commission_imobiliaria},
                {"nome": manager, "tipo": "Corretor", "valor": commission_corretor, "comissionado_valor": commission_corretor}
            ],
            "clientes": [
                {
                    "nome": client_buyer, 
                    "papel": "Comprador", 
                    "tipo_pessoa": "Física", 
                    "genero": "Feminino" if i % 2 == 0 else "Masculino", 
                    "data_nascimento": "1991-03-25", 
                    "endereco_bairro": bairro
                },
                {
                    "nome": client_seller, 
                    "papel": "Vendedor", 
                    "tipo_pessoa": "Física", 
                    "genero": "Masculino" if i % 2 == 0 else "Feminino", 
                    "data_nascimento": "1980-07-14", 
                    "endereco_bairro": bairro
                }
            ]
        }
        # Data Quality group assignment
        if i % 10 == 0:
            tx["agente_gestor_grupos_a_que_pertence"] = []
        elif i % 10 == 1:
            tx["agente_gestor_grupos_a_que_pertence"] = ["group_branch_1"]
        elif i % 10 == 2:
            tx["agente_gestor_grupos_a_que_pertence"] = ["group_unmapped_1"]
        elif i % 10 == 3:
            tx["agente_gestor_grupos_a_que_pertence"] = ["group_team_1"]
        elif i % 10 == 4:
            tx["agente_gestor_grupos_a_que_pertence"] = ["group_team_2"]
        elif i % 10 == 5:
            tx["agente_gestor_grupos_a_que_pertence"] = ["group_team_1", "group_branch_1"]
        elif i % 10 == 6:
            tx["agente_gestor_grupos_a_que_pertence"] = None
            tx["agente_gestor_grupos_a_que_pertence1"] = "Equipe Meta"
            tx["agente_gestor_grupos_a_que_pertence2"] = ""
            tx["agente_gestor_grupos_a_que_pertence3"] = ""
        elif i % 10 == 7:
            if i == 7:
                tx["agente_gestor_grupos_a_que_pertence"] = ["group_team_1"]
            else:
                tx["agente_gestor_grupos_a_que_pertence"] = ["group_team_2"]
        elif i % 10 == 8:
            tx["agente_gestor_grupos_a_que_pertence"] = ["group_team_1"]
        else:
            tx["agente_gestor_grupos_a_que_pertence"] = ["group_team_2"]
            
        tx["agente_gestor_grupo_filial"] = "Filial Florianópolis" if i % 2 == 0 else "Filial Campeche"

        transactions.append(tx)
        
    return transactions

MOCK_TRANSACTIONS = generate_mock_transactions()
