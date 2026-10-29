-- Extend immutable protection evidence to Gate TESTNET without removing history.
-- Apply transactionally after reviewed backup and staging migration checks.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE capital_cipher.oms_order_protections
    DROP CONSTRAINT ck_oms_protection_exchange,
    ADD CONSTRAINT ck_oms_protection_exchange CHECK (exchange IN ('BYBIT', 'GATE'));

CREATE OR REPLACE FUNCTION capital_cipher.capture_gate_order_protection()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $function$
DECLARE
    risk_payload jsonb;
    captured_stop numeric(38,18);
    captured_target numeric(38,18);
    captured_leverage numeric(20,8);
BEGIN
    IF NEW.exchange <> 'GATE' OR NEW.environment <> 'TESTNET' THEN
        RETURN NEW;
    END IF;

    SELECT evaluation.payload INTO risk_payload
      FROM capital_cipher.order_approvals approval
      JOIN capital_cipher.risk_evaluations evaluation
        ON evaluation.evaluation_id = approval.evaluation_id
     WHERE approval.approval_id = NEW.approval_id
       AND approval.risk_check_id = NEW.risk_check_id
       AND evaluation.approved = true;
    IF risk_payload IS NULL THEN
        RAISE EXCEPTION 'Gate TESTNET order has no approved durable risk evidence' USING errcode = '55000';
    END IF;
    captured_stop := NULLIF(risk_payload ->> 'stop_loss','')::numeric;
    captured_target := NULLIF(risk_payload ->> 'take_profit','')::numeric;
    captured_leverage := NULLIF(risk_payload ->> 'leverage','')::numeric;
    IF captured_stop IS NULL OR captured_target IS NULL OR captured_leverage IS NULL
       OR captured_stop::text = 'NaN' OR captured_target::text = 'NaN'
       OR captured_leverage IS DISTINCT FROM NEW.leverage OR captured_leverage <> 1 THEN
        RAISE EXCEPTION 'Gate TESTNET requires durable SL/TP and approved 1x leverage' USING errcode = '55000';
    END IF;
    IF (NEW.side = 'BUY' AND NOT (captured_stop < NEW.reference_price AND NEW.reference_price < captured_target))
       OR (NEW.side = 'SELL' AND NOT (captured_target < NEW.reference_price AND NEW.reference_price < captured_stop)) THEN
        RAISE EXCEPTION 'Gate TESTNET protection does not bracket reference price' USING errcode = '55000';
    END IF;
    INSERT INTO capital_cipher.oms_order_protections
        (oms_order_id,approval_id,exchange,environment,symbol,side,execution_intent,reduce_only,
         stop_loss,take_profit,leverage,reference_price,created_at)
    VALUES (NEW.oms_order_id,NEW.approval_id,NEW.exchange,NEW.environment,NEW.symbol,NEW.side,
            'ENTRY',false,captured_stop,captured_target,captured_leverage,NEW.reference_price,now());
    RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION capital_cipher.capture_gate_order_protection() FROM public;
CREATE TRIGGER trg_capture_gate_order_protection
    AFTER INSERT ON capital_cipher.oms_orders
    FOR EACH ROW EXECUTE FUNCTION capital_cipher.capture_gate_order_protection();
