from services.validation_csv_loader import load_pipeimob_transactions, load_vista_gains


def test_vista_loader_keeps_only_gain_and_drops_client_pii(tmp_path):
    source = tmp_path / "vista.csv"
    source.write_text(
        "Título;Valor potencial;Cliente;E-mail do Cliente;Telefone do Cliente;"
        "Corretores;Etapa;Encerramento do negócio;Status\n"
        "44258 - Imóvel;605000.00;Cliente Privado;privado@example.com;9999;"
        "Corretor Comercial;Fechamento;18/08/2026;Ganho\n"
        "99999 - Aberto;100000.00;Outro;outro@example.com;8888;"
        "Outro Corretor;Fechamento;;Em aberto\n",
        encoding="utf-8",
    )

    gains = load_vista_gains(source)

    assert gains == [
        {
            "deal_id": "44258",
            "property_code": "44258",
            "gain_date": "18/08/2026",
            "deal_value": "605000.00",
            "commercial_broker_name": "Corretor Comercial",
            "stage_name": "Fechamento",
        }
    ]
    assert "Cliente" not in gains[0]
    assert "email" not in str(gains[0]).lower()


def test_pipeimob_loader_uses_only_reconciliation_fields(tmp_path):
    source = tmp_path / "pipe.csv"
    source.write_text(
        "ID transação Pipeimob;Código contrato;Código imóvel;Data CCV;"
        "Valor contrato;Agente gestor;Origem\n"
        "tx-1;ccv-1;44258;18/08/2026;605000.00;Fiscal;Site\n",
        encoding="utf-8",
    )

    rows = load_pipeimob_transactions(source)

    assert rows == [
        {
            "transacao_unique_id_pipeimob": "tx-1",
            "codigo_contrato": "ccv-1",
            "codigo_imovel": "44258",
            "data_assinatura_ccv": "18/08/2026",
            "valor_contrato": "605000.00",
            "agente_gestor": "Fiscal",
        }
    ]
