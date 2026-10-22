-- 009_toques.sql — Fork: los 3 toques de seguimiento después de una cotización.
-- `cotizada`: Diana ya mandó una cotización en esta conversación, o le ofreció
-- la visita técnica a un carro varado (desde ahí, el seguimiento son los toques
-- y no el empujón único de la IA).
-- `urgente`: el cliente dijo que el carro está varado (el toque 1 sale antes).
-- `toque`: cuántos toques salieron desde el último mensaje del cliente.
-- `toques_desde`: ese último mensaje; los tiempos se cuentan desde ahí, que
-- es también desde donde Meta cuenta la ventana de 24 h. Idempotente.

ALTER TABLE bot_conversation
  ADD COLUMN IF NOT EXISTS cotizada BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS urgente BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS toque SMALLINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS toques_desde TIMESTAMPTZ;
