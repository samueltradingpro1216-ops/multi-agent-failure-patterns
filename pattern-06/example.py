"""
Pattern 06 — Silent NameError in try/except
Demontre comment un try/except generique peut masquer un NameError,
rendant une fonctionnalite critique silencieusement morte.

Usage: python example.py
"""
import ast
import textwrap


# === LE BUG ===

class BuggyAgentProcessor:
    """
    BUG: 'config' est utilise avant son assignation.
    Le except Exception masque le NameError.
    La branche force_shutdown() ne s'execute JAMAIS.
    """

    def process(self, agent_id: str, is_disabled: bool):
        try:
            # Etape 1: verifier si l'agent est disabled
            if is_disabled:
                # BUG: 'config' n'existe pas encore a cette ligne
                if config.get("has_active_task"):
                    self.force_shutdown(agent_id)
                    return "SHUTDOWN"

            # Etape 2: lire la config (30 lignes plus bas dans le vrai code)
            config = self.read_config(agent_id)

            # Etape 3: traitement normal
            return f"PROCESSED {agent_id}"

        except Exception:
            # Le NameError est avale silencieusement ici
            pass

        return "SKIPPED"

    def read_config(self, agent_id: str) -> dict:
        return {"has_active_task": True, "agent": agent_id}

    def force_shutdown(self, agent_id: str):
        print(f"  FORCE SHUTDOWN {agent_id}")


# === LA CORRECTION ===

class FixedAgentProcessor:
    """
    FIX: config est lu AVANT d'etre utilise.
    Les exceptions sont loggees, pas avalees.
    """

    def process(self, agent_id: str, is_disabled: bool):
        # Initialiser les variables en debut de scope
        config = None

        try:
            # FIX: lire la config EN PREMIER
            config = self.read_config(agent_id)

            # Maintenant on peut l'utiliser
            if is_disabled:
                if config.get("has_active_task"):
                    self.force_shutdown(agent_id)
                    return "SHUTDOWN"

            return f"PROCESSED {agent_id}"

        except Exception as e:
            # FIX: logger l'erreur au lieu de l'avaler
            print(f"  ERROR processing {agent_id}: {type(e).__name__}: {e}")
            return "ERROR"

    def read_config(self, agent_id: str) -> dict:
        return {"has_active_task": True, "agent": agent_id}

    def force_shutdown(self, agent_id: str):
        print(f"  FORCE SHUTDOWN {agent_id}")


# === DETECTION ===

def detect_silent_except(source_code: str) -> list[dict]:
    """
    Detecte les blocs try/except qui avalent les erreurs silencieusement.
    Cherche: except Exception: pass, except: pass, except Exception as e: pass
    """
    alerts = []
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return alerts

    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            # Verifier chaque handler except
            handlers = getattr(node, 'handlers', [])
            for handler in handlers:
                # except Exception ou except (bare)
                is_broad = (
                    handler.type is None  # bare except
                    or (isinstance(handler.type, ast.Name)
                        and handler.type.id in ("Exception", "BaseException"))
                )

                if not is_broad:
                    continue

                # Verifier si le body est juste 'pass' ou vide
                body = handler.body
                is_silent = (
                    len(body) == 1
                    and isinstance(body[0], (ast.Pass, ast.Expr))
                )

                # Aussi detecter: except Exception as e:\n    pass
                if is_silent or (len(body) == 1 and isinstance(body[0], ast.Pass)):
                    except_type = "bare except" if handler.type is None else "except Exception"
                    alerts.append({
                        "line": handler.lineno,
                        "type": except_type,
                        "message": f"Ligne {handler.lineno}: {except_type} silencieux (pass/vide)"
                    })

    return alerts


def detect_use_before_assign(source_code: str) -> list[dict]:
    """
    Detection simplifiee: cherche les variables utilisees dans un try
    avant leur assignation dans le meme bloc.
    """
    alerts = []
    lines = source_code.split("\n")

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Pattern: if <var>.get(...) ou <var>[...] dans un try
        # suivi plus bas de: <var> = ...
        # Heuristique simple pour la demo
        if "except Exception" in stripped and "pass" in stripped:
            alerts.append({
                "line": i,
                "message": f"Ligne {i}: 'except Exception: pass' detecte"
            })
        elif stripped.startswith("except") and stripped.endswith("pass"):
            alerts.append({
                "line": i,
                "message": f"Ligne {i}: except silencieux detecte"
            })

    return alerts


# === DEMONSTRATION ===

if __name__ == "__main__":
    print("=" * 60)
    print("Pattern 06 -- Silent NameError in try/except")
    print("=" * 60)

    # --- Version buggee ---
    print("\n--- Version BUGGEE ---")
    buggy = BuggyAgentProcessor()

    # Agent disabled avec tache active: devrait faire SHUTDOWN
    result = buggy.process("agent-alpha", is_disabled=True)
    print(f"  Agent disabled + tache active: {result}")
    print(f"  Attendu: SHUTDOWN")
    print(f"  -> force_shutdown() ne s'est JAMAIS executee!")

    # Agent normal
    result = buggy.process("agent-beta", is_disabled=False)
    print(f"  Agent normal: {result}")

    # --- Version corrigee ---
    print("\n--- Version CORRIGEE ---")
    fixed = FixedAgentProcessor()

    result = fixed.process("agent-alpha", is_disabled=True)
    print(f"  Agent disabled + tache active: {result}")

    result = fixed.process("agent-beta", is_disabled=False)
    print(f"  Agent normal: {result}")

    # --- Detection statique ---
    print("\n--- Detection statique ---")

    buggy_code = textwrap.dedent("""\
    def process(agent_id):
        try:
            if config.get("key"):
                do_something()
            config = load_config()
        except Exception:
            pass

    def other():
        try:
            result = risky_call()
        except ValueError:
            handle_error()
    """)

    alerts = detect_silent_except(buggy_code)
    if alerts:
        print(f"  Alertes trouvees dans le code:")
        for alert in alerts:
            print(f"    {alert['message']}")
    else:
        print("  Aucune alerte")

    # --- Pourquoi c'est dangereux ---
    print("\n--- Pourquoi c'est dangereux ---")
    print("  Le NameError est INVISIBLE:")

    caught_errors = []
    try:
        # Simuler le bug
        undefined_var.get("key")  # noqa: F821
    except Exception as e:
        caught_errors.append(f"{type(e).__name__}: {e}")

    print(f"  Exception capturee: {caught_errors[0]}")
    print(f"  Sans le except, Python aurait crashe.")
    print(f"  Avec le except: silence total, code mort.")
