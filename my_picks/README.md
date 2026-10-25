# My picks

Add one line per pick to `picks.csv` (edit it right on GitHub). Columns: `date,sport,player,market,price,note`.
`date` is the game date (YYYY-MM-DD, Eastern). `sport` is nfl, nhl or nba. `market` for NFL is `td` (anytime), `first_td`, `rec_yds`, `rec`, `rush_yds`, `rush_att`, `pass_yds`, `pass_td`, `td2`, `long_rec`, `qb_td`;
NHL `goal`, `points`, `assists`, `shots3`; NBA `pts25`, `reb10`, `ast8`, `tpm4`, `pra35`, `dd`. `price` is the American price you took (blank if you did not price it). `note` is anything you want to group by (tight end day, numerology, injury boost...).

`my-picks.yml` grades the file every morning against the shadow logs and writes the report to the run summary and to `bbe-history`.
