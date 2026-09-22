                        certainty=Certainty.CONFIRMED if upgrade else "",
                        followup_relevant=True,
                    )
                )
                return intents
        until = obs.observed_at + timedelta(hours=lifecycle.TTL_NOW_HOURS)
        intents.append(
            TransitionIntent(
                action=IntentAction.ESTABLISH,
                reason="symptom active (SYMPTOM_LIFECYCLE)",
                evidence=evidence,
                category="health",
                key=obs.key,
                value="active",
                status=StateStatus.ACTIVE,
                certainty=Certainty.CONFIRMED,
                valid_from=obs.observed_at,
                valid_until=until,
                relevant_until=until,
                followup_relevant=True,
                snapshot_priority=5,
            )
        )
        return intents

    @staticmethod
    def _latest_health_for_key(states: List[State], key: str) -> Optional[State]:
        matches = [s for s in states if s.category == "health" and s.key == key]
        if not matches:
            return None
        return max(matches, key=lambda s: s.updated_at)

# Backward compatibility for existing callers/tests.
InferenceEngine = TransitionPlanner
