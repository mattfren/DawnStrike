# Command Center X2 Story Models

Models are defined in `intraday_scanner/v2/command_center_x2/story_models.py`.
They normalize existing artifacts into AppStoryModel, MonthCalendarModel,
DayStoryModel, StrategyStoryModel, PaperTradeStoryModel, NoPicksStoryModel, and
AutomationStoryModel. Unknown values stay `n/a`; missing artifacts become
warnings; shadow challengers remain shadow; and strategy validation is never
inferred from UI state.
