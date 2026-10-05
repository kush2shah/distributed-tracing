from managed_deepagents import define_deep_agent

from tools.weather import mda_lookup_weather

agent = define_deep_agent(
    name="probe",
    model="openai:gpt-4.1-mini",
    tools=[mda_lookup_weather],
)
