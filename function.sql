create or replace function get_customer_fullname(cid INT) returns text as $$
declare fullname text;
begin
	select concat(first_name, ' ',  last_name)
	into fullname
	from customer
	where customer_id = cid;
	
	return fullname;
end;
$$ language plpgsql;

-- select get_customer_fullname(33);

drop function if exists get_films_by_category(category_name text);
create or replace function get_films_by_category(category_name text)
returns table (
    film_id int,
    title varchar(255),
    release_year year,
    rental_rate numeric
) as $$
begin
    return query
    select
        f.film_id,
        f.title,
        f.release_year,
        f.rental_rate
    from film f
    join film_category fc on f.film_id = fc.film_id
    join category c on fc.category_id = c.category_id
    where c.name = category_name;
end;
$$ language plpgsql;

-- select * from get_films_by_category('Action');

create or replace function calculate_rental_duration(v_rental_id int)
returns int as $$
declare
	v_rental_date timestamp;
	v_return_date timestamp;
	v_days int;
begin
	select rental_date, coalesce(return_date, current_date)
	into v_rental_date, v_return_date
	from rental
	where rental_id = v_rental_id;

	if not found then
		return null;
	end if;

	v_days := extract(day from (v_return_date - v_rental_date));

	return v_days;

end;
$$ language plpgsql;

-- select calculate_rental_duration(100);

create or replace function get_customer_total_payment(p_customer_id int)
returns numeric
as $$
declare 
	total_payment numeric;
begin 
	select coalesce(sum(amount), 0.00)
    into total_payment
    from payment
    where customer_id = p_customer_id;

    return total_payment;
end;
$$ language plpgsql;

-- select get_customer_total_payment(44);


drop function if exists get_top_customers(int);

create or replace function get_top_customers(p_limit int)
returns table (
    customer_id int,
    full_name text,
    total_payment numeric
) as $$
begin
    return querys
    select
        c.customer_id,
        concat_ws(' ', c.first_name, c.last_name)::text as full_name,
        coalesce(sum(p.amount), 0.00) as total_payment
    from customer c
    join payment p on c.customer_id = p.customer_id
    group by c.customer_id, c.first_name, c.last_name
    order by total_payment desc
    limit p_limit;
end;
$$ language plpgsql;


