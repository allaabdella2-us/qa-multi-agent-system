import { useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import Button from "../components/Button";
import { ApiError, createOrder, type OrderItem } from "../api";

const PENDING_ORDER_KEY = "corvid.pending_order";

interface ItemDraft {
  sku: string;
  description: string;
  quantity: string;
  unitPrice: string;
}

const EMPTY_REFERENCE = "";

function emptyItem(): ItemDraft {
  return { sku: "", description: "", quantity: "1", unitPrice: "" };
}

function emptyItems(): ItemDraft[] {
  return [emptyItem()];
}

function isBlank(item: ItemDraft): boolean {
  return (
    item.sku.trim() === "" &&
    item.description.trim() === "" &&
    item.unitPrice.trim() === "" &&
    (item.quantity.trim() === "" || item.quantity.trim() === "1")
  );
}

function toOrderItem(item: ItemDraft): OrderItem | null {
  const sku = item.sku.trim();
  const description = item.description.trim();
  const quantity = Number(item.quantity);
  const unitPrice = Number(item.unitPrice);

  if (!sku || !description) return null;
  if (!Number.isInteger(quantity) || quantity < 1) return null;
  if (!Number.isFinite(unitPrice) || unitPrice < 0 || item.unitPrice.trim() === "") return null;

  return {
    sku,
    description,
    quantity,
    unit_price_cents: Math.round(unitPrice * 100),
  };
}

function validate(reference: string, items: ItemDraft[]): string | null {
  if (!reference.trim()) {
    return "Enter a reference for this order.";
  }

  const filled = items.filter((item) => !isBlank(item));
  if (filled.length === 0) {
    return "Add at least one item to this order.";
  }

  const parsed = filled.map(toOrderItem);
  if (parsed.some((item) => item === null)) {
    return "Every item needs a SKU, a description, a whole-number quantity of at least 1, and a unit price.";
  }

  return null;
}

export default function NewOrder() {
  const navigate = useNavigate();
  const [reference, setReference] = useState(EMPTY_REFERENCE);
  const [items, setItems] = useState<ItemDraft[]>(emptyItems);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  function updateItem(index: number, patch: Partial<ItemDraft>) {
    setItems((current) =>
      current.map((item, position) => (position === index ? { ...item, ...patch } : item)),
    );
  }

  function addItem() {
    setItems((current) => [...current, emptyItem()]);
  }

  function removeItem(index: number) {
    setItems((current) =>
      current.length === 1 ? current : current.filter((_, position) => position !== index),
    );
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    const problem = validate(reference, items);
    if (problem) {
      setReference(EMPTY_REFERENCE);
      setItems(emptyItems());
      setError(problem);
      return;
    }

    const payload = {
      reference: reference.trim(),
      items: items
        .filter((item) => !isBlank(item))
        .map(toOrderItem)
        .filter((item): item is OrderItem => item !== null),
    };

    setSubmitting(true);
    try {
      const created = await createOrder(payload);
      // The create response does not always echo the lines back, so keep the
      // ones we submitted for the review step.
      const order = { ...created, items: created.items ?? payload.items };
      try {
        window.sessionStorage.setItem(PENDING_ORDER_KEY, JSON.stringify(order));
      } catch {
        // Session storage is a convenience for reloads; the router state below
        // carries the order for the normal navigation.
      }
      navigate("/checkout/review", { state: { order } });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Could not reach the server. Check your connection and try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="page">
      <div className="page-header">
        <h1 className="page-title">New order</h1>
      </div>

      <form className="card" onSubmit={handleSubmit} data-testid="new-order-form">
        {error ? (
          <p className="alert alert-error" role="alert" data-testid="new-order-error">
            {error}
          </p>
        ) : null}

        <div className="field">
          <label className="field-label" htmlFor="order-reference">
            Reference
          </label>
          <input
            id="order-reference"
            className="field-input"
            type="text"
            maxLength={64}
            value={reference}
            onChange={(event) => setReference(event.target.value)}
            data-testid="new-order-reference"
          />
        </div>

        <h2 className="section-title">Items</h2>

        {items.map((item, index) => (
          <div className="item-row" key={index} data-testid={`item-row-${index}`}>
            <div className="field">
              <label className="field-label" htmlFor={`item-sku-${index}`}>
                SKU
              </label>
              <input
                id={`item-sku-${index}`}
                className="field-input"
                type="text"
                value={item.sku}
                onChange={(event) => updateItem(index, { sku: event.target.value })}
                data-testid={`item-sku-${index}`}
              />
            </div>

            <div className="field field-grow">
              <label className="field-label" htmlFor={`item-description-${index}`}>
                Description
              </label>
              <input
                id={`item-description-${index}`}
                className="field-input"
                type="text"
                value={item.description}
                onChange={(event) => updateItem(index, { description: event.target.value })}
                data-testid={`item-description-${index}`}
              />
            </div>

            <div className="field field-narrow">
              <label className="field-label" htmlFor={`item-quantity-${index}`}>
                Quantity
              </label>
              <input
                id={`item-quantity-${index}`}
                className="field-input"
                type="number"
                min={1}
                step={1}
                value={item.quantity}
                onChange={(event) => updateItem(index, { quantity: event.target.value })}
                data-testid={`item-quantity-${index}`}
              />
            </div>

            <div className="field field-narrow">
              <label className="field-label" htmlFor={`item-price-${index}`}>
                Unit price
              </label>
              <input
                id={`item-price-${index}`}
                className="field-input"
                type="number"
                min={0}
                step="0.01"
                value={item.unitPrice}
                onChange={(event) => updateItem(index, { unitPrice: event.target.value })}
                data-testid={`item-price-${index}`}
              />
            </div>

            <Button
              variant="secondary"
              onClick={() => removeItem(index)}
              disabled={items.length === 1}
              aria-label={`Remove item ${index + 1}`}
              data-testid={`remove-item-${index}`}
            >
              Remove
            </Button>
          </div>
        ))}

        <div className="form-actions">
          <Button variant="secondary" onClick={addItem} data-testid="add-item-button">
            Add item
          </Button>
          <Button
            type="submit"
            variant="primary"
            disabled={submitting}
            data-testid="new-order-submit"
          >
            {submitting ? "Creating…" : "Create order"}
          </Button>
        </div>
      </form>
    </section>
  );
}
